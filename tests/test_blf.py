"""BLF 解析器测试。

测试内联一个最小 BLF **写入器**（与 Vector BLF / python-can 的布局一致），
用来构造各种边界样本；另有一份落盘的 `tests/data/sample_v1.blf`
（帧序列与 sample_v1.asc 相同）作为固定回归基准 ——
写入器若悄悄漂移，落盘样本会把差异暴露出来。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from automotive_dev_support.core.skeleton import ArtifactKind, ArtifactRole, JobContext
from automotive_dev_support.core.skeleton.capability import SourceCapability
from automotive_dev_support.core.skeleton.datasource import DataSource
from automotive_dev_support.core.skeleton.artifact import ArtifactRef
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.parser_blf import BlfParser

SAMPLE_DATA = Path(__file__).parent / "data"
CTX = JobContext(job_id="test-blf")

# -- 与 parser 一致的格式常量（写入测试样本用） ------------------------------

FILE_HEADER = struct.Struct("<4sLBBBBBBBBQQLL8H8H")
OBJ_HEADER_BASE = struct.Struct("<4sHHLL")
OBJ_HEADER_V1 = struct.Struct("<LHHQ")
OBJ_HEADER_V2 = struct.Struct("<LBxHQ8x")
LOG_CONTAINER = struct.Struct("<H6xL4x")
CAN_MSG = struct.Struct("<HBBL8s")
CAN_FD_MSG = struct.Struct("<HBBLLBBB5x64s")
CAN_ERROR_EXT = struct.Struct("<HHLBBBxLLH2x8s")

CAN_MESSAGE = 1
LOG_CONTAINER_TYPE = 10
CAN_ERROR_EXT_TYPE = 73
CAN_FD_MESSAGE = 100
GLOBAL_MARKER = 96
CAN_FD_MESSAGE_64 = 101
TIME_ONE_NANS = 2
TIME_TEN_MICS = 1
CAN_MSG_EXT = 0x80000000
REMOTE_FLAG = 0x80
DIR_FLAG = 0x01
NO_COMPRESSION = 0
ZLIB_DEFLATE = 2


class _MemorySource(DataSource):
    """内存数据源 —— 只实现读取路径，用于喂给解析器。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    @property
    def uri(self) -> str:
        return "memory://sample.blf"

    def capabilities(self):
        return frozenset({SourceCapability.READ, SourceCapability.STREAM})

    def read_all(self) -> bytes:
        return self._data

    def stream(self, chunk_size: int | None = None):
        size = chunk_size or (1 << 16)
        for i in range(0, len(self._data), size):
            yield self._data[i : i + size]

    def artifact_ref(self):
        return ArtifactRef(uri=self.uri)


def _can_msg(channel, flags, dlc, can_id, data):
    return CAN_MSG.pack(channel, flags, dlc, can_id, data.ljust(8, b"\x00"))


def _can_fd(channel, flags, dlc, can_id, fd_flags, data):
    return CAN_FD_MSG.pack(
        channel, flags, dlc, can_id, 0, 0, fd_flags, len(data),
        data.ljust(64, b"\x00"),
    )


def _err_ext(channel, dlc, can_id, data):
    return CAN_ERROR_EXT.pack(channel, 0, 0, 0, 0, dlc, 0, can_id, 0, data.ljust(8, b"\x00"))


def _object(obj_type, payload, ticks, time_flags=TIME_ONE_NANS, version=1):
    header_size = 16 + (OBJ_HEADER_V1.size if version == 1 else OBJ_HEADER_V2.size)
    obj_size = header_size + len(payload)
    body = OBJ_HEADER_BASE.pack(b"LOBJ", header_size, version, obj_size, obj_type)
    if version == 1:
        body += OBJ_HEADER_V1.pack(time_flags, 0, 0, ticks)
    else:
        body += OBJ_HEADER_V2.pack(time_flags, 0, 0, ticks)
    body += payload
    return body + b"\x00" * ((4 - obj_size % 4) % 4)


def _blf(
    objects: list[bytes],
    *,
    start=(2026, 10, 15, 4, 13, 41, 21, 123),
    stop=(2026, 10, 15, 4, 13, 41, 31, 456),
    compression=NO_COMPRESSION,
) -> bytes:
    """把对象列表打包成一个 BLF 文件（单容器）。"""
    data = b"".join(objects)
    if compression == ZLIB_DEFLATE:
        payload_data = zlib.compress(data)
    else:
        payload_data = data
    container_payload = LOG_CONTAINER.pack(compression, len(data)) + payload_data
    container_size = 16 + len(container_payload)
    container = OBJ_HEADER_BASE.pack(b"LOBJ", 16, 1, container_size, LOG_CONTAINER_TYPE)
    container += container_payload
    container += b"\x00" * ((4 - container_size % 4) % 4)

    body = container
    file_size = 144 + len(body)
    uncompressed = 144 + 16 + 16 + len(data)
    header = FILE_HEADER.pack(
        b"LOGG", 144, 5, 0, 0, 0, 2, 6, 8, 1,
        file_size, uncompressed, len(objects), 0,
        *start, *stop,
    )
    return header + b"\x00" * (144 - len(header)) + body


class TestBlfSample:
    def test_parses_committed_sample(self):
        outcome = BlfParser().parse(FileSource(SAMPLE_DATA / "sample_v1.blf"), CTX)
        m = outcome.document
        assert outcome.artifact.kind is ArtifactKind.BLF
        assert outcome.artifact.role is ArtifactRole.MEASUREMENT
        assert m.meta["frames"] == 11
        assert m.meta["containers"] == 1
        assert m.meta["error_frames"] == 2
        assert m.meta["fd_frames"] == 2
        assert m.meta["time_base"] == "absolute"
        assert m.meta["finalized"] is True
        assert not outcome.warnings

    def test_frame_fields(self):
        m = BlfParser().parse(FileSource(SAMPLE_DATA / "sample_v1.blf"), CTX).document
        first = m.events[0]
        assert first.channel == 1
        assert first.frame_id == 0x850
        assert first.direction == "rx"
        assert first.data == bytes.fromhex("1122334455667788")
        tx = m.events[3]
        assert tx.direction == "tx"
        ext = m.events[4]
        assert ext.frame_id == 0x100 and ext.extended is True
        remote = m.events[5]
        assert remote.kind == "remote" and remote.dlc == 8 and remote.data == b""
        err = m.events[6]
        assert err.kind == "error" and err.frame_id == 0
        err_id = m.events[7]
        assert err_id.kind == "error" and err_id.frame_id == 0x5F4

    def test_can_fd_frames(self):
        m = BlfParser().parse(FileSource(SAMPLE_DATA / "sample_v1.blf"), CTX).document
        fd = m.events[8]
        assert fd.is_fd and fd.frame_id == 0x300 and len(fd.data) == 12
        fd2 = m.events[9]
        assert fd2.is_fd and fd2.channel == 2 and len(fd2.data) == 8

    def test_absolute_timestamps(self):
        m = BlfParser().parse(FileSource(SAMPLE_DATA / "sample_v1.blf"), CTX).document
        from datetime import datetime, timezone

        anchor = datetime(2026, 10, 15, 13, 41, 21, 123000, tzinfo=timezone.utc).timestamp()
        assert m.start_time is not None
        assert m.start_time.timestamp() == pytest.approx(anchor)
        assert m.events[0].t == pytest.approx(anchor)
        assert m.events[1].t == pytest.approx(anchor + 0.001)
        assert m.time_range() == pytest.approx((anchor, anchor + 0.010))

    def test_stats_are_complete(self):
        m = BlfParser().parse(FileSource(SAMPLE_DATA / "sample_v1.blf"), CTX).document
        counts = m.frame_counts()
        assert counts[(1, 0x850)] == 3
        assert counts[(1, 0x5F4)] == 1
        stat = m.event_stats[(1, 0x850)]
        assert stat.count == 3
        assert stat.bytes == 8 + 8 + 8


class TestBlfVariants:
    def test_zlib_compressed_container(self):
        obj = _object(CAN_MESSAGE, _can_msg(1, 0, 2, 0x123, b"\xaa\xbb"), 5_000_000)
        outcome = BlfParser().parse(_MemorySource(_blf([obj], compression=ZLIB_DEFLATE)), CTX)
        m = outcome.document
        assert m.meta["frames"] == 1
        assert m.events[0].data == b"\xaa\xbb"

    def test_object_header_v2(self):
        obj = _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x321, b"\x07"), 3_000_000, version=2)
        m = BlfParser().parse(_MemorySource(_blf([obj])), CTX).document
        assert m.meta["frames"] == 1
        assert m.events[0].frame_id == 0x321

    def test_ten_microsecond_ticks(self):
        # 1e-5 秒/tick：5 万 tick = 0.5 秒
        obj = _object(
            CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x01"), 50_000,
            time_flags=TIME_TEN_MICS,
        )
        m = BlfParser().parse(_MemorySource(_blf([obj])), CTX).document
        anchor = m.meta["anchor"]
        assert m.events[0].t == pytest.approx(anchor + 0.5)

    def test_unknown_object_types_are_counted(self):
        good = _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x01"), 0)
        marker = _object(GLOBAL_MARKER, b"\x00" * 40, 1_000_000)
        fd64 = _object(CAN_FD_MESSAGE_64, b"\x00" * 48, 2_000_000)
        outcome = BlfParser().parse(_MemorySource(_blf([good, marker, fd64])), CTX)
        m = outcome.document
        assert m.meta["frames"] == 1
        assert m.meta["skipped_objects"] == {"GLOBAL_MARKER": 1, "CAN_FD_MESSAGE_64": 1}
        assert any("不支持的对象" in w for w in outcome.warnings)

    def test_unfinalized_file_falls_back_to_relative(self):
        # 起止时间全零 = 记录未收尾；绝对时刻不可恢复，转相对首帧
        good = _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x01"), 10_000_000)
        later = _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x02"), 12_500_000)
        zero = (0, 0, 0, 0, 0, 0, 0, 0)
        outcome = BlfParser().parse(
            _MemorySource(_blf([good, later], start=zero, stop=zero)), CTX
        )
        m = outcome.document
        assert m.meta["finalized"] is False
        assert m.meta["time_base"] == "relative"
        assert any("未正常收尾" in w for w in outcome.warnings)
        assert m.events[0].t == 0.0
        assert m.events[1].t == pytest.approx(0.0025)

    def test_non_blf_file(self):
        outcome = BlfParser().parse(_MemorySource(b"not a blf file at all"), CTX)
        assert outcome.document.meta["frames"] == 0
        assert any("不是 BLF 文件" in w for w in outcome.warnings)

    def test_truncated_file_keeps_what_it_has(self):
        full = _blf([
            _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x01"), 0),
            _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x02"), 1_000_000),
        ])
        outcome = BlfParser().parse(_MemorySource(full[:-20]), CTX)
        m = outcome.document
        assert m.meta["frames"] >= 1
        assert m.meta["truncated"] is True
        assert any("截断" in w for w in outcome.warnings)

    def test_max_events_cap_keeps_full_stats(self):
        objects = [
            _object(CAN_MESSAGE, _can_msg(1, 0, 1, 0x100, b"\x01"), i * 1_000_000)
            for i in range(50)
        ]
        parser = BlfParser(max_events=10)
        outcome = parser.parse(_MemorySource(_blf(objects)), CTX)
        m = outcome.document
        assert len(m.events) == 10
        assert m.meta["frames"] == 50
        assert m.frame_counts()[(1, 0x100)] == 50
        assert any("帧体仅保留前 10 帧" in w for w in outcome.warnings)
