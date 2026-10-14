"""内存镜像链路：装配 → 解析（HEX / S19）→ 按描述取值。

这条链路守三件容易quietly出错的事：

1. **checksum 坏的记录不能加载。** 坏字节进了标定值，
   比对结果是错的而且看不出哪里错 —— 比报错严重得多。
2. **镜像没覆盖的地址不能读。** `read()` 对空洞填 0xFF，
   不查覆盖就把 Flash 擦除态当成真实数据。
3. **HEX 与 S19 必须产出同一个东西。** 两者格式毫无共性
   （checksum 一个是二补数一个是一补数），但装配结果必须一致 ——
   这是「新增格式 = 新增一个文件夹」的回归防线。
"""

from __future__ import annotations

import pytest

from conftest import PLUGINS_DIR, SAMPLE_DATA
from automotive_dev_support.core.models.calibration import (
    CalibrationModel,
    ChangeKind,
    Characteristic,
    CharacteristicType,
    CompuMethod,
    DataType,
    bind_image,
)
from automotive_dev_support.core.models.common import SymbolPath
from automotive_dev_support.core.models.memory import AddressSpace, ImageAssembler
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, ArtifactRole, JobContext
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.diff_calibration import compare_models
from automotive_dev_support.plugins.parser_a2l import A2lParser
from automotive_dev_support.plugins.parser_hex import HexParser
from automotive_dev_support.plugins.parser_s19 import S19Parser

CTX = JobContext(job_id="test")


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load_all([PLUGINS_DIR])
    return reg


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="ascii", newline="\n")
    return FileSource(path)


# ---------------------------------------------------------------------------
# 装配器（格式无关）
# ---------------------------------------------------------------------------


class TestImageAssembler:
    def test_contiguous_chunks_merge_into_one_segment(self):
        asm = ImageAssembler()
        asm.add(0x1000, b"\x01\x02")
        asm.add(0x1002, b"\x03\x04")
        space = asm.build()
        assert len(space.segments) == 1
        assert space.segments[0].start == 0x1000
        assert space.segments[0].data == b"\x01\x02\x03\x04"

    def test_gap_stays_a_gap(self):
        asm = ImageAssembler()
        asm.add(0x1000, b"\x01")
        asm.add(0x2000, b"\x02")
        space = asm.build()
        assert len(space.segments) == 2
        assert space.coverage()["gaps"] == [(0x1001, 0x2000)]

    def test_overlap_warns_and_last_wins(self):
        asm = ImageAssembler()
        asm.add(0x1000, b"\x01\x02\x03")
        asm.add(0x1002, b"\x09\x09")
        space = asm.build()
        assert asm.warnings and "重叠" in asm.warnings[0]
        assert space.read(0x1000, 4) == b"\x01\x02\x09\x09"

    def test_empty_chunk_is_ignored(self):
        asm = ImageAssembler()
        asm.add(0x1000, b"")
        assert asm.build().segments == []

    def test_negative_address_rejected(self):
        asm = ImageAssembler()
        asm.add(-1, b"\x01")
        assert asm.build().segments == []
        assert asm.warnings


# ---------------------------------------------------------------------------
# Intel HEX
# ---------------------------------------------------------------------------


class TestHexParser:
    def test_parses_sample_and_reads_values(self):
        space = HexParser().parse(FileSource(SAMPLE_DATA / "sample_v1.hex"), CTX).document
        assert isinstance(space, AddressSpace)
        assert space.read(0x80001234, 2) == (850).to_bytes(2, "big")
        assert space.read(0x80001238, 2) == (350).to_bytes(2, "big", signed=True)
        # 16x8 的 MAP，首元素 0，末元素 (127*7)%5000 = 889
        assert space.read(0x80002000, 2) == b"\x00\x00"
        assert space.read(0x80002000 + 254, 2) == (889).to_bytes(2, "big")

    def test_extended_linear_base_applies_to_following_records(self):
        text = ":0200000480007A\n:02123400035263\n:00000001FF\n"
        space = HexParser().parse(_write_hex(text), CTX).document
        assert space.read(0x80001234, 2) == (850).to_bytes(2, "big")

    def test_bad_checksum_record_is_not_loaded(self):
        # 正常记录 :02 1234 00 0352 63；把 checksum 改坏
        text = ":0200000480007A\n:02123400035200\n:00000001FF\n"
        outcome = HexParser().parse(_write_hex(text), CTX)
        assert outcome.warnings and "checksum" in outcome.warnings[0]
        # 坏记录没加载 —— 读出来的是「不存在」，不是假数据
        assert not outcome.document.covers(0x80001234, 2)

    def test_missing_eof_is_reported(self):
        text = ":0200000480007A\n:02123400035263\n"
        outcome = HexParser().parse(_write_hex(text), CTX)
        assert any("EOF" in w for w in outcome.warnings)

    def test_garbage_lines_are_skipped_with_warning(self):
        text = "这不是 hex\n:0200000480007A\n:02123400035263\n:00000001FF\n"
        outcome = HexParser().parse(_write_hex(text), CTX)
        assert any("起始符" in w for w in outcome.warnings)
        assert outcome.document.read(0x80001234, 2) == (850).to_bytes(2, "big")
    def test_artifact_meta_reports_coverage(self):
        outcome = HexParser().parse(FileSource(SAMPLE_DATA / "sample_v1.hex"), CTX)
        assert outcome.artifact.meta["bytes"] > 0
        assert outcome.artifact.meta["skipped"] == 0
        assert outcome.artifact.role is ArtifactRole.IMAGE


def _write_bytes(name: str, text: str, suffix: str):
    """把内存里的文本落到临时文件。测试内联构造小样本用。

    按字节写：垃圾行测试要混入非 ASCII 字符，
    模拟真实世界里被手改过的文件。
    """
    import tempfile
    from pathlib import Path

    fd = tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False)
    fd.write(text.encode("utf-8"))
    fd.close()
    return FileSource(Path(fd.name))


def _write_hex(text: str):
    return _write_bytes("tmp", text, ".hex")


def _write_s19(text: str):
    return _write_bytes("tmp", text, ".s19")


# ---------------------------------------------------------------------------
# Motorola S-Record
# ---------------------------------------------------------------------------


class TestS19Parser:
    def test_parses_sample(self):
        space = S19Parser().parse(FileSource(SAMPLE_DATA / "sample_v1.s19"), CTX).document
        assert space.read(0x80001234, 2) == (850).to_bytes(2, "big")
        assert space.read(0x80001238, 2) == (350).to_bytes(2, "big", signed=True)

    def test_same_content_same_result_as_hex(self):
        """两种格式描述同一份内存时，装配结果必须逐字节一致。"""
        hex_space = HexParser().parse(FileSource(SAMPLE_DATA / "sample_v1.hex"), CTX).document
        s19_space = S19Parser().parse(FileSource(SAMPLE_DATA / "sample_v1.s19"), CTX).document
        for addr in (0x80001234, 0x80001236, 0x80001238, 0x8000123A, 0x80002000):
            assert hex_space.read(addr, 2) == s19_space.read(addr, 2)
        assert hex_space.read(0x80002000, 128) == s19_space.read(0x80002000, 128)

    def test_bad_checksum_record_is_not_loaded(self):
        # S3 记录：count=07, addr=80001234, data=0352, checksum=DD（正确值）
        # 把 checksum 改坏成 00
        text = "S30780001234035200\nS70500000000FA\n"
        outcome = S19Parser().parse(_write_s19(text), CTX)
        assert outcome.warnings and "checksum" in outcome.warnings[0]
        assert not outcome.document.covers(0x80001234, 2)

    def test_header_becomes_label(self):
        outcome = S19Parser().parse(FileSource(SAMPLE_DATA / "sample_v1.s19"), CTX)
        assert outcome.artifact.label == "DEMO ENGINE MODULE"


# ---------------------------------------------------------------------------
# 按描述从镜像取值
# ---------------------------------------------------------------------------


def _parse_a2l(name: str) -> CalibrationModel:
    return A2lParser().parse(FileSource(SAMPLE_DATA / name), CTX).document


def _parse_hex(name: str) -> AddressSpace:
    return HexParser().parse(FileSource(SAMPLE_DATA / name), CTX).document


class TestBindImage:
    def test_values_extracted_with_physical_conversion(self):
        model = _parse_a2l("sample_v1.a2l")
        image = _parse_hex("sample_v1.hex")
        bound, binding = bind_image(model, image)
        assert binding.missing_in_values == []
        assert binding.is_clean
        # Kp_Speed：UWORD，CM_None 直通
        assert bound.get("Kp_Speed").physical_values() == [850.0]
        # Temp_Offset：raw 350，CM_TempLinear(0.1x-40) → -5.0 degC
        assert bound.get("Temp_Offset").physical_values() == [-5.0]
        # Torque_Map：128 个元素全部取到
        map_values = bound.get("Torque_Map").physical_values()
        assert len(map_values) == 128
        assert map_values[0] == 0.0

    def test_uncovered_characteristic_reported_not_faked(self):
        """镜像里没有的地址，必须报缺失 —— 绝不能拿 0xFF 冒充。"""
        model = _parse_a2l("sample_v1.a2l")
        # 只含 Kp_Speed 的残缺镜像
        asm = ImageAssembler()
        asm.add(0x80001234, (850).to_bytes(2, "big"))
        bound, binding = bind_image(model, asm.build())
        assert "Kp_Speed" in binding.matched
        assert "Ki_Speed" in binding.missing_in_values
        assert bound.get("Ki_Speed").raw_values is None
        assert any("没有从镜像取到值" in w for w in binding.warnings)

    def test_characteristic_without_address_reported(self):
        model = CalibrationModel(
            characteristics={
                "No_Addr": Characteristic(
                    path=SymbolPath.parse("No_Addr"),
                    data_type=DataType.UINT16,
                )
            }
        )
        asm = ImageAssembler()
        asm.add(0x1000, b"\x01\x02")
        _, binding = bind_image(model, asm.build())
        assert binding.missing_in_values == ["No_Addr"]

    def test_little_endian_is_honored(self):
        model = CalibrationModel(
            characteristics={
                "Le_Val": Characteristic(
                    path=SymbolPath.parse("Le_Val"),
                    data_type=DataType.UINT16,
                    address=0x1000,
                )
            }
        )
        asm = ImageAssembler()
        asm.add(0x1000, (0x0352).to_bytes(2, "little"))
        bound, _ = bind_image(model, asm.build(), byte_order="little")
        assert bound.get("Le_Val").physical_values() == [850.0]
        # 同一份字节按大端读必须是另一个数 —— 证明字节序真的生效了
        bound_be, _ = bind_image(model, asm.build(), byte_order="big")
        assert bound_be.get("Le_Val").physical_values() == [0x5203]

    def test_float_roundtrip(self):
        model = CalibrationModel(
            characteristics={
                "F": Characteristic(
                    path=SymbolPath.parse("F"),
                    data_type=DataType.FLOAT32,
                    address=0x2000,
                )
            }
        )
        asm = ImageAssembler()
        import struct

        asm.add(0x2000, struct.pack(">f", 3.5))
        bound, _ = bind_image(model, asm.build())
        assert bound.get("F").physical_values() == [pytest.approx(3.5)]


# ---------------------------------------------------------------------------
# 端到端：A2L + HEX → 绑定 → 比对
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_compare_two_hex_backed_versions(self):
        v1, _ = bind_image(_parse_a2l("sample_v1.a2l"), _parse_hex("sample_v1.hex"))
        v2, _ = bind_image(_parse_a2l("sample_v2.a2l"), _parse_hex("sample_v2.hex"))
        cs = compare_models(
            v1, v2, baseline_label="v1", candidate_label="v2"
        )

        by_path = {c.path: c for c in cs.changed}
        added = {c.path for c in cs.added}
        removed = {c.path for c in cs.removed}

        assert added == {"Fuel_Cut_Threshold"}
        assert removed == {"Temp_Offset"}
        # 数值真的变了；地址也漂了，但地址漂移只在「没有其它变化」时
        # 才单独成条（否则只是附带信息）—— 地址记录在 address_before/after 里
        kp = by_path["Kp_Speed"]
        assert ChangeKind.VALUES_CHANGED in kp.kinds
        assert kp.address_before == 0x80001234
        assert kp.address_after == 0x80001244
        # 转换方法变了但数值没变 → 只报属性变化
        idle = by_path["Idle_Base"]
        assert ChangeKind.ATTRS_CHANGED in idle.kinds
        assert ChangeKind.VALUES_CHANGED not in idle.kinds
        # 上限值变化 → 属性变化，不是数值变化
        ki = by_path["Ki_Speed"]
        assert ChangeKind.ATTRS_CHANGED in ki.kinds
        assert ChangeKind.VALUES_CHANGED not in ki.kinds
        # 完全未变的不出现在报告里
        assert "Torque_Map" not in by_path
        assert cs.meaningful_changes()

    def test_plugins_discoverable(self):
        reg = PluginRegistry()
        reg.load_all([PLUGINS_DIR])
        ids = {p.manifest.id for p in reg}
        assert {"parser-hex", "parser-s19"} <= ids
        assert reg.parsers_for(ArtifactKind.HEX)
        assert reg.parsers_for(ArtifactKind.S19)
