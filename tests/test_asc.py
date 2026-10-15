"""ASC 解析器测试。

样例 `tests/data/sample_v1.asc` 是仿 CANoe 输出手工排的，
刻意覆盖：数据帧 / 远程帧 / 两种错误帧 / 带符号名与不带符号名的
CAN FD / 扩展 ID / Tx 方向 / 统计行 / Start of measurement。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from automotive_dev_support.core.skeleton import ArtifactKind, ArtifactRole, JobContext
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.parser_asc import AscParser

SAMPLE_DATA = Path(__file__).parent / "data"
CTX = JobContext(job_id="test-asc")


def _parse(text: str, name: str = "inline.asc"):
    import tempfile

    fd = tempfile.NamedTemporaryFile(
        "w", suffix=".asc", delete=False, encoding="utf-8", newline="\n"
    )
    fd.write(text)
    fd.close()
    outcome = AscParser().parse(FileSource(Path(fd.name)), CTX)
    return outcome


class TestAscSample:
    def test_parses_all_frame_kinds(self):
        outcome = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX)
        m = outcome.document
        assert outcome.artifact.kind is ArtifactKind.ASC
        assert outcome.artifact.role is ArtifactRole.MEASUREMENT
        assert m.meta["frames"] == 11
        assert m.meta["error_frames"] == 2
        assert m.meta["remote_frames"] == 1
        assert m.meta["fd_frames"] == 2
        # 统计行、Start of measurement、End Triggerblock 被跳过并计数
        assert m.meta["skipped_lines"] == 3
        assert any("跳过 3 个" in w for w in outcome.warnings)

    def test_data_frame_fields(self):
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        ev = m.events[0]
        assert ev.channel == 1
        assert ev.frame_id == 0x850
        assert ev.direction == "rx"
        assert ev.kind == "can"
        assert ev.data == bytes.fromhex("1122334455667788")
        assert ev.dlc == 8

    def test_extended_id_and_tx(self):
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        ext = m.events[4]
        assert ext.frame_id == 0x100
        assert ext.extended is True
        assert ext.data == b"\xaa\xbb"
        tx = m.events[3]
        assert tx.direction == "tx"

    def test_remote_frame_carries_dlc_only(self):
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        remote = m.events[5]
        assert remote.kind == "remote"
        assert remote.dlc == 8
        assert remote.data == b""

    def test_both_error_frame_forms(self):
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        no_id, with_id = m.events[6], m.events[7]
        assert no_id.kind == "error" and no_id.frame_id == 0
        assert with_id.kind == "error" and with_id.frame_id == 0x5F4

    def test_can_fd_with_symbolic_name(self):
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        fd = m.events[8]
        assert fd.is_fd and fd.frame_id == 0x300
        assert len(fd.data) == 12
        assert fd.data[0] == 0x01 and fd.data[-1] == 0x0C

    def test_can_fd_without_symbolic_name(self):
        # 符号名为空 → 行里出现连续空格，不能按列号切
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        fd = m.events[9]
        assert fd.is_fd and fd.frame_id == 0x180
        assert fd.channel == 2
        assert len(fd.data) == 8
        assert fd.data == bytes.fromhex("1020304050607080")

    def test_absolute_timestamps_use_trigger_anchor(self):
        outcome = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX)
        m = outcome.document
        assert m.meta["time_base"] == "absolute"
        anchor = datetime(2026, 10, 15, 13, 41, 21, 123000).timestamp()
        assert m.meta["anchor"] == pytest.approx(anchor)
        assert m.events[0].t == pytest.approx(anchor + 0.0)
        assert m.events[1].t == pytest.approx(anchor + 0.001)

    def test_events_by_id(self):
        m = AscParser().parse(FileSource(SAMPLE_DATA / "sample_v1.asc"), CTX).document
        counts = m.frame_counts()
        assert counts[(1, 0x850)] == 3  # 2 Rx + 1 Tx
        assert counts[(1, 0x5F4)] == 1


class TestAscVariants:
    HEADER = (
        "date Oct 15 2026\n"
        "time 1:41:21.123 pm\n"
        "base {base} timestamps {ts}\n"
        "no internal events logged\n"
        "Begin Triggerblock Oct 15 2026 1:41:21.123 pm\n"
    )

    def test_relative_timestamps_pass_through(self):
        text = self.HEADER.format(base="hex", ts="relative") + (
            "   0.500000 1  100             Rx   d 1 0A\n"
            "End Triggerblock\n"
        )
        m = _parse(text).document
        assert m.meta["time_base"] == "relative"
        assert m.events[0].t == 0.5

    def test_dec_base(self):
        text = self.HEADER.format(base="dec", ts="relative") + (
            "   0.100000 1  2136            Rx   d 2 17 34\n"
            "End Triggerblock\n"
        )
        m = _parse(text).document
        assert m.events[0].frame_id == 2136
        # dec 基准下 "17"/"34" 是十进制数，不是十六进制
        assert m.events[0].data == bytes([17, 34])

    def test_absolute_without_anchor_falls_back_with_warning(self):
        # 既没有 Triggerblock 行，也没有 date/time 行 —— 锚点无从谈起
        text = (
            "base hex  timestamps absolute\n"
            "   0.250000 1  100             Rx   d 1 0A\n"
        )
        outcome = _parse(text)
        assert any("找不到起始锚点" in w for w in outcome.warnings)
        assert outcome.document.events[0].t == 0.25

    def test_anchor_from_date_and_time_lines(self):
        # 没有 Triggerblock，但 date + time 行齐了 —— 照样能锚定
        text = (
            "date Oct 15 2026\n"
            "time 1:41:21.123 pm\n"
            "base hex  timestamps absolute\n"
            "   0.250000 1  100             Rx   d 1 0A\n"
        )
        outcome = _parse(text)
        assert not any("锚点" in w for w in outcome.warnings)
        anchor = datetime(2026, 10, 15, 13, 41, 21, 123000).timestamp()
        assert outcome.document.events[0].t == pytest.approx(anchor + 0.25)

    def test_date_only_anchor_is_not_trusted(self):
        # 只有日期没有时间：午夜锚点会把下午的记录整体挪走，不如不用
        text = (
            "date Oct 15 2026\n"
            "base hex  timestamps absolute\n"
            "   0.250000 1  100             Rx   d 1 0A\n"
        )
        outcome = _parse(text)
        assert any("找不到起始锚点" in w for w in outcome.warnings)
        assert outcome.document.events[0].t == 0.25

    def test_bad_lines_skipped_not_fatal(self):
        text = self.HEADER.format(base="hex", ts="relative") + (
            "这不是 asc 行\n"
            "   0.100000 1  100             Rx   d 1 0A\n"
            "   0.200000 1  ZZZ             Rx   d 1 0A\n"
            "   0.300000 1  100             Rx   d 4 01\n"
            "End Triggerblock\n"
        )
        outcome = _parse(text)
        m = outcome.document
        assert len(m.events) == 1  # 只有第一行是好的
        # 中文行 + ZZZ ID 行 + 数据不齐行 + End Triggerblock
        assert m.meta["skipped_lines"] == 4
        assert any("报文 ID 无法解析" in w for w in outcome.warnings)
        assert any("声明 4 字节" in w for w in outcome.warnings)

    def test_empty_file(self):
        outcome = _parse("")
        assert outcome.document.meta["frames"] == 0
        assert outcome.document.time_range() is None

    def test_lines_split_across_chunks(self):
        # 流式按行读：行被块边界切开时必须能拼回去
        from automotive_dev_support.core.skeleton.datasource import DataSource

        class ChunkedSource(DataSource):
            def __init__(self, data: bytes, chunk: int = 7) -> None:
                self._data = data
                self._chunk = chunk

            @property
            def uri(self) -> str:
                return "memory://chunked.asc"

            def capabilities(self):
                from automotive_dev_support.core.skeleton.capability import SourceCapability

                return frozenset({SourceCapability.READ, SourceCapability.STREAM})

            def read_all(self) -> bytes:
                return self._data

            def stream(self, chunk_size: int | None = None):
                size = chunk_size or self._chunk
                for i in range(0, len(self._data), size):
                    yield self._data[i : i + size]

            def artifact_ref(self):
                from automotive_dev_support.core.skeleton.artifact import ArtifactRef

                return ArtifactRef(uri=self.uri)

        text = self.HEADER.format(base="hex", ts="relative") + (
            "   0.100000 1  100             Rx   d 1 0A\n"
            "   0.200000 1  200             Rx   d 2 0B 0C\n"
        )
        outcome = AscParser().parse(ChunkedSource(text.encode("ascii")), CTX)
        assert len(outcome.document.events) == 2
        assert outcome.document.events[1].data == b"\x0b\x0c"
