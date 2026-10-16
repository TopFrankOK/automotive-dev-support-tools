"""Intel HEX 写回的测试。

**这里最要紧的是第一条**：一字不改时，输出必须与原文件**字节级相同**。

这条看着简单，其实决定了整个写回功能值不值得信任。
做不到它，写回就只是"重新导出一遍"：`git diff` 显示整文件改动，
改了哪个量、改了多少，人看不出来 —— 标定文件是要进版本库、要过评审的。

第二条同样要紧：改一个字节，**只有那一行**变。
"""

from __future__ import annotations

from conftest import PLUGINS_DIR, SAMPLE_DATA
from automotive_dev_support.core.models.memory import AddressSpace, ImageAssembler
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, JobContext
from automotive_dev_support.core.skeleton.capability import PluginCapability
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.parser_hex import HexParser
from automotive_dev_support.plugins.writer_hex import HexWriter

CTX = JobContext(job_id="test")


def _parse(name: str) -> AddressSpace:
    return HexParser().parse(FileSource(SAMPLE_DATA / name), CTX).document


class TestLossless:
    def test_unchanged_image_reproduces_the_file_byte_for_byte(self):
        """样例文件是 CRLF；行尾由调用方探测后传入，才能逐字节还原。"""
        for name in ("sample_v1.hex", "sample_v2.hex"):
            original = (SAMPLE_DATA / name).read_bytes()
            produced = HexWriter().serialize(_parse(name), {"line_ending": "\r\n"})
            assert produced == original, f"{name} 写回后与原文件不同"

    def test_line_ending_defaults_to_lf(self):
        image = _parse("sample_v1.hex")
        assert b"\r" not in HexWriter().serialize(image, {})

    def test_one_changed_byte_changes_exactly_one_line(self):
        image = _parse("sample_v1.hex")
        before = HexWriter().serialize(image, {}).decode().splitlines()

        image.write(0x80001234, b"\x09\x99")
        after = HexWriter().serialize(image, {}).decode().splitlines()

        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(changed) == 1
        spot = changed[0]
        assert before[:spot] == after[:spot], "改动之前的行被重写了"
        assert before[spot + 1 :] == after[spot + 1 :], "改动之后的行被重写了"

    def test_real_roundtrip_through_the_parser(self, tmp_path):
        """写出去 → 再解析回来，字节与段必须一致。"""
        image = _parse("sample_v1.hex")
        path = tmp_path / "out.hex"
        path.write_bytes(HexWriter().serialize(image, {}))

        outcome = HexParser().parse(FileSource(path), CTX)
        assert not outcome.warnings
        assert outcome.document.read(0x80001234, 8) == image.read(0x80001234, 8)
        assert outcome.document.coverage()["bytes"] == image.coverage()["bytes"]


class TestFormat:
    def test_every_line_has_a_valid_checksum(self):
        """checksum 是二补数：整行字节和为 0。"""
        image = _parse("sample_v1.hex")
        for line in HexWriter().serialize(image, {}).decode().splitlines():
            assert line.startswith(":")
            assert sum(bytes.fromhex(line[1:])) & 0xFF == 0

    def test_data_records_stay_within_the_0xff_limit(self):
        asm = ImageAssembler()
        asm.add(0x1000, b"\xaa" * 600)  # 无边界信息 → 由插件按上限切
        for line in HexWriter().serialize(asm.build(), {}).decode().splitlines():
            assert int(line[1:3], 16) <= 0xFF

    def test_high_address_emits_a_linear_address_record(self):
        asm = ImageAssembler()
        asm.add(0x0001_0000, b"\xaa" * 4)
        out = HexWriter().serialize(asm.build(), {}).decode()
        assert out.startswith(":020000040001F9"), out

    def test_linear_base_is_not_repeated(self):
        """同一 64K 页内的后续记录不该重复发 04。"""
        asm = ImageAssembler()
        asm.add(0x0001_0000, b"\xaa" * 4)
        asm.add(0x0001_0010, b"\xbb" * 4)
        out = HexWriter().serialize(asm.build(), {}).decode()
        assert out.count(":02000004") == 1

    def test_empty_image_is_still_valid(self):
        assert HexWriter().serialize(AddressSpace(), {}) == b":00000001FF\n"

    def test_output_is_uppercase_with_lf_endings(self):
        asm = ImageAssembler()
        asm.add(0x1000, b"\xab\xcd")
        out = HexWriter().serialize(asm.build(), {})
        assert out.endswith(b"\n")
        assert b"\r" not in out
        assert out.decode().strip().upper() == out.decode().strip()


class TestPlugin:
    def test_is_discoverable_as_a_writer(self):
        registry = PluginRegistry()
        registry.load_all([PLUGINS_DIR])
        found = [p.manifest.id for p in registry.by_capability(PluginCapability.WRITER)]
        assert "writer-hex" in found
        loaded = registry.get("writer-hex")
        assert loaded is not None and loaded.usable

    def test_declares_hex_kind(self):
        assert ArtifactKind.HEX in HexWriter.kinds
