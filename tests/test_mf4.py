"""MDF4 解析器测试。

样例文件由 `tests/tools/make_mf4_samples.py` 生成（随仓库提交，
二进制样例必须可复现）。边界用例用同一个 Builder 现造小文件。
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import pytest

from automotive_dev_support.core.models.measurement import SignalKind
from automotive_dev_support.core.skeleton import JobContext
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.parser_mf4 import Mdf4Parser
from automotive_dev_support.services import ensure_time_axis, load_series

sys.path.insert(0, str(Path(__file__).parent / "tools"))
from make_mf4_samples import Builder, cn_block  # noqa: E402

SAMPLE_DATA = Path(__file__).parent / "data"
CTX = JobContext(job_id="test-mf4")


def _parse(path: Path):
    return Mdf4Parser().parse(FileSource(path), CTX)


def _parse_bytes(blob: bytes, name: str = "inline.mf4"):
    import tempfile

    fd = tempfile.NamedTemporaryFile("wb", suffix=".mf4", delete=False)
    fd.write(blob)
    fd.close()
    return _parse(Path(fd.name))


def _write(blob: bytes) -> Path:
    import tempfile

    fd = tempfile.NamedTemporaryFile("wb", suffix=".mf4", delete=False)
    fd.write(blob)
    fd.close()
    return Path(fd.name)


# ---------------------------------------------------------------------------
# 随仓库的样例：sorted + unsorted 两条路径
# ---------------------------------------------------------------------------


class TestMf4Sample:
    def test_parses_committed_sample(self):
        outcome = _parse(SAMPLE_DATA / "sample_v1.mf4")
        assert outcome.warnings == []
        m = outcome.document
        # 两个数据组：sorted 测量组 + unsorted 总线组（两个 CG）
        assert len(m.groups) == 3
        assert m.start_time is not None
        assert m.start_time.year == 2026
        assert m.start_time.hour == 4
        meta = outcome.artifact.meta
        assert meta["version"] == "4.10"
        assert meta["data_groups"] == 2
        assert meta["channel_groups"] == 3

    def test_sorted_group_is_lazy(self):
        m = _parse(SAMPLE_DATA / "sample_v1.mf4").document
        grp = m.groups[0]
        assert grp.master is not None and grp.master.name == "time"
        names = {s.name for s in grp.signals}
        assert names == {"EngineSpeed", "CoolantTemp"}
        for sig in [grp.master, *grp.signals]:
            assert sig.spec is not None
            assert sig.location is not None
            assert sig.location.count == 100
            assert not sig.loaded  # 索引阶段不取值

    def test_sorted_group_values_are_correct(self):
        src = FileSource(SAMPLE_DATA / "sample_v1.mf4")
        m = Mdf4Parser().parse(src, CTX).document
        grp = m.groups[0]
        secs = ensure_time_axis(src, grp)
        assert secs is not None
        assert secs[0] == 0.0
        assert secs[1] == pytest.approx(0.01)
        assert secs[99] == pytest.approx(0.99)

        speed = load_series(src, grp.signal("EngineSpeed"))
        assert speed[0] == pytest.approx(800.0)
        assert speed[10] == pytest.approx(800.0 + 100.0 * math.sin(1.0))
        # 单位来自 TX 块
        assert grp.signal("EngineSpeed").unit == "rpm"
        assert grp.signal("CoolantTemp").unit == "degC"

        temp = load_series(src, grp.signal("CoolantTemp"))
        assert temp[0] == 900
        assert temp[99] == 999
        assert m.duration() == pytest.approx(0.99)

    def test_unsorted_groups_are_extracted(self):
        m = _parse(SAMPLE_DATA / "sample_v1.mf4").document
        bus1, bus2 = m.groups[1], m.groups[2]
        for grp in (bus1, bus2):
            assert grp.master is not None
            assert all(s.loaded for s in grp.signals)  # 未排序组解析时取值
            assert all(s.values is not None for s in grp.signals)
        # 交错存放的记录按 record_id 正确归属
        ids1 = list(bus1.signal("CAN_ID").values)
        assert ids1 == [0x100 + i for i in range(10)]
        ids2 = list(bus2.signal("CAN_ID").values)
        assert ids2 == [0x200 + i for i in range(10)]
        # 数据字节按小端还原（bytes(range(1,9)) = 01..08 → 0x0807060504030201）
        assert bus1.signal("DataBytes").values[0] == 0x0807060504030201
        assert bus2.signal("DataBytes").values[0] == 0xDDCCBBAA
        # 两组时间不同（CG2 晚 5 ms），证明没有串组
        assert bus2.master.values[0] - bus1.master.values[0] == 5_000_000

    def test_registry_loads_plugin(self):
        from automotive_dev_support.core.ports import PluginRegistry

        registry = PluginRegistry()
        registry.load_all([Path("src/automotive_dev_support/plugins")])
        ids = {p.manifest.id for p in registry}
        assert "parser-mf4" in ids


# ---------------------------------------------------------------------------
# 边界情形：坏签名 / 版本不符 / 不支持的通道类型
# ---------------------------------------------------------------------------


def _minimal_file(
    *,
    channels: list[dict],
    record: bytes,
    cycles: int = 1,
    samples_bytes: int | None = None,
    cg_flags: int = 0,
    record_id_len: int = 0,
    version: bytes = b"4.10",
) -> bytes:
    """用 Builder 现造一个「单 DG 单 CG」的小文件。

    channels: [{name, unit, channel_type, data_type, bit_count, byte_offset}]
    """
    b = Builder()
    idb = b.alloc(64)
    b.put(
        idb,
        b"MDF " + b"\x00" * 4 + version + b"test" + b"\x00" * 4 + b"\x00" * 40,
    )
    hd = b.alloc(104)
    b.put(hd, struct.pack("<4sIQQ", b"##HD", 0, 104, 6) + b"\x00" * (6 * 8))
    b.put(hd + 72, struct.pack("<qhh", 0, 0, 0) + bytes(4) + b"\x00" * 16)

    dg = b.alloc(64)
    cg = b.alloc(104)
    dt = b.alloc(24 + len(record))
    b.put(dt, struct.pack("<4sIQQ", b"##DT", 0, 24 + len(record), 0) + record)

    ch_addrs = []
    for spec in channels:
        addr = cn_block(
            b,
            next_ch=0,
            name_addr=b.tx(spec["name"]),
            unit_addr=b.tx(spec.get("unit", "")),
            channel_type=spec["channel_type"],
            data_type=spec["data_type"],
            bit_count=spec["bit_count"],
            byte_offset=spec["byte_offset"],
        )
        ch_addrs.append(addr)
    for a, nxt in zip(ch_addrs, ch_addrs[1:]):
        b.put(a + 24, struct.pack("<Q", nxt))

    b.put(dg, struct.pack("<4sIQQ", b"##DG", 0, 64, 4) + b"\x00" * 32)
    b.put(dg + 56, bytes([record_id_len]) + b"\x00" * 7)
    b.link(dg, 32, cg)
    b.link(dg, 40, dt)

    stride = samples_bytes if samples_bytes is not None else len(record)
    b.put(
        cg,
        struct.pack("<4sIQQ", b"##CG", 0, 104, 6)
        + b"\x00" * 48
        + struct.pack("<QQ", 0, cycles)
        + struct.pack("<HH", cg_flags, 0)
        + struct.pack("<III", 0, stride, 0),
    )
    b.link(cg, 32, ch_addrs[0])
    b.link(hd, 24, dg)
    return b.seal()


class TestMf4EdgeCases:
    def test_rejects_non_mdf_signature(self):
        outcome = _parse_bytes(b"NOTMDF" + b"\x00" * 200)
        assert any("IDBLOCK" in w for w in outcome.warnings)
        assert outcome.document.groups == []

    def test_rejects_mdf3_version(self):
        record = struct.pack("<Q", 1)
        blob = _minimal_file(
            channels=[
                {
                    "name": "t",
                    "channel_type": 2,
                    "data_type": 1,
                    "bit_count": 64,
                    "byte_offset": 0,
                }
            ],
            record=record,
            version=b"3.30",
        )
        outcome = _parse_bytes(blob)
        assert any("3.30" in w for w in outcome.warnings)
        assert outcome.document.groups == []

    def test_string_channel_is_skipped_with_warning(self):
        record = b"hello\x00\x00\x00"
        blob = _minimal_file(
            channels=[
                {
                    "name": "text",
                    "channel_type": 0,
                    "data_type": 8,  # UTF-8 字符串
                    "bit_count": 64,
                    "byte_offset": 0,
                }
            ],
            record=record,
            samples_bytes=8,
        )
        outcome = _parse_bytes(blob)
        assert any("字符串" in w for w in outcome.warnings)
        assert outcome.document.groups == []

    def test_bit_packed_channel_is_skipped_with_warning(self):
        record = b"\x0f\x00"
        blob = _minimal_file(
            channels=[
                {
                    "name": "packed",
                    "channel_type": 0,
                    "data_type": 1,
                    "bit_count": 4,
                    "byte_offset": 0,
                }
            ],
            record=record,
            samples_bytes=2,
        )
        outcome = _parse_bytes(blob)
        assert any("8/16/32/64" in w for w in outcome.warnings)
        assert outcome.document.groups == []

    def test_vlsd_group_is_skipped_with_warning(self):
        record = struct.pack("<Q", 1)
        blob = _minimal_file(
            channels=[
                {
                    "name": "t",
                    "channel_type": 2,
                    "data_type": 1,
                    "bit_count": 64,
                    "byte_offset": 0,
                }
            ],
            record=record,
            cg_flags=1,  # FLAG_CG_VLSD
        )
        outcome = _parse_bytes(blob)
        assert any("VLSD" in w for w in outcome.warnings)

    def test_truncated_data_area_clamps_count_with_warning(self):
        # 声明 5 条记录但只写了 1 条的数据
        record = struct.pack("<Q", 7)
        blob = _minimal_file(
            channels=[
                {
                    "name": "t",
                    "channel_type": 2,
                    "data_type": 1,
                    "bit_count": 64,
                    "byte_offset": 0,
                }
            ],
            record=record,
            cycles=5,
        )
        outcome = _parse_bytes(blob)
        assert any("截断" in w for w in outcome.warnings)
        grp = outcome.document.groups[0]
        assert grp.master is not None
        assert grp.master.location.count == 1

    def test_group_without_master_warns_but_keeps_signals(self):
        record = struct.pack("<d", 3.5)
        blob = _minimal_file(
            channels=[
                {
                    "name": "lonely",
                    "channel_type": 0,  # 普通通道，没有主通道
                    "data_type": 3,
                    "bit_count": 64,
                    "byte_offset": 0,
                }
            ],
            record=record,
        )
        outcome = _parse_bytes(blob)
        assert any("没有时间主通道" in w for w in outcome.warnings)
        grp = outcome.document.groups[0]
        assert grp.master is None
        assert grp.signals[0].name == "lonely"
        assert grp.signals[0].kind == SignalKind.CONTINUOUS

    def test_channel_beyond_sample_area_is_skipped(self):
        record = struct.pack("<Q", 1)
        blob = _minimal_file(
            channels=[
                {
                    "name": "t",
                    "channel_type": 2,
                    "data_type": 1,
                    "bit_count": 64,
                    "byte_offset": 0,
                },
                {
                    "name": "outside",
                    "channel_type": 0,
                    "data_type": 3,
                    "bit_count": 64,
                    "byte_offset": 64,  # 超出 8 字节样本区
                },
            ],
            record=record,
        )
        outcome = _parse_bytes(blob)
        assert any("超出记录样本区" in w for w in outcome.warnings)
        names = {s.name for s in outcome.document.groups[0].signals}
        assert "outside" not in names

    def test_dz_block_instead_of_dt_warns(self):
        """数据块是 DZ（压缩）而不是 DT —— 显式告警，不静默跳过。"""
        b = Builder()
        idb = b.alloc(64)
        b.put(idb, b"MDF " + b"\x00" * 4 + b"4.10" + b"test" + b"\x00" * 4 + b"\x00" * 40)
        hd = b.alloc(104)
        b.put(hd, struct.pack("<4sIQQ", b"##HD", 0, 104, 6) + b"\x00" * (6 * 8))
        b.put(hd + 72, struct.pack("<qhh", 0, 0, 0) + bytes(4) + b"\x00" * 16)
        dg = b.alloc(64)
        cg = b.alloc(104)
        dz = b.alloc(24)
        b.put(dz, struct.pack("<4sIQQ", b"##DZ", 0, 24, 0))
        tx = b.tx("t")
        cn = cn_block(
            b, next_ch=0, name_addr=tx, unit_addr=tx,
            channel_type=2, data_type=1, bit_count=64, byte_offset=0,
        )
        b.put(dg, struct.pack("<4sIQQ", b"##DG", 0, 64, 4) + b"\x00" * 32)
        b.put(dg + 56, bytes([0]) + b"\x00" * 7)
        b.link(dg, 32, cg)
        b.link(dg, 40, dz)
        b.put(
            cg,
            struct.pack("<4sIQQ", b"##CG", 0, 104, 6)
            + b"\x00" * 48
            + struct.pack("<QQ", 0, 1)
            + struct.pack("<HH", 0, 0)
            + struct.pack("<III", 0, 8, 0),
        )
        b.link(cg, 32, cn)
        b.link(hd, 24, dg)
        outcome = _parse_bytes(b.seal())
        assert any("##DZ" in w or "DZ" in w for w in outcome.warnings)
