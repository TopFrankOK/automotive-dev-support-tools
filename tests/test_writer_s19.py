"""writer-s19 的测试。

## 这里钉住的是两套格式最容易互相抄错的地方

**S-Record 的 checksum 是一补数**（`0xFF - (sum & 0xFF)`），
**Intel HEX 的是二补数**（`(~sum + 1) & 0xFF`）。
照抄的后果是：文件能写出来、能被别的工具读，
但刷进 ECU 会被拒 —— 而且不报错，只能到台架上才发现。

## 以及两条 HEX 没有的麻烦

`AddressSpace` 只有地址与字节，而 S-Record 有两样信息在模型之外：

  - **S0 头记录**（厂商文本标签）
  - **终止记录类型与启动地址**（S7 / S8 / S9）

HEX 的扩展线性地址记录能从地址自动重建，所以 HEX 能字节级还原；
S19 若不管这两样，一写回就丢掉第一行。
解决办法是把原文件字节经 `ctx["source_bytes"]` 传给插件，
由插件自己还原 —— 格式知识留在插件里，服务层只是传递字节。
"""

from __future__ import annotations

from conftest import SAMPLE_DATA
from automotive_dev_support.core.models.memory import AddressSpace, ImageAssembler
from automotive_dev_support.core.skeleton.job import JobContext
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.parser_s19 import S19Parser
from automotive_dev_support.plugins.writer_hex import HexWriter
from automotive_dev_support.plugins.writer_s19 import S19Writer

SAMPLE = "sample_v1.s19"


def _parse(name: str = SAMPLE) -> AddressSpace:
    return S19Parser().parse(FileSource(SAMPLE_DATA / name), JobContext(job_id="t")).document


def _image(address: int, data: bytes) -> AddressSpace:
    """程序构造的镜像（没有记录边界 —— 走默认切分）。"""
    asm = ImageAssembler()
    asm.add(address, data)
    return asm.build()


def _raw(name: str = SAMPLE) -> bytes:
    return (SAMPLE_DATA / name).read_bytes()


class TestLosslessRoundTrip:
    def test_unchanged_image_reproduces_the_file_byte_for_byte(self):
        """一字不改 → 输出与原文件**字节级相同**（含 S0 头与 S7 终止记录）。"""
        raw = _raw()
        produced = S19Writer().serialize(_parse(), {"source_bytes": raw})
        assert produced == raw

    def test_changing_one_byte_changes_only_that_line(self):
        """改一个量 → 只有那一行变。行号不能漂移，否则 diff 会糊成一片。"""
        raw = _raw()
        image = _parse()
        image.write(0x80001234, b"\x09\x99")

        produced = S19Writer().serialize(image, {"source_bytes": raw})
        before = raw.decode("ascii").splitlines()
        after = produced.decode("ascii").splitlines()

        assert len(before) == len(after)
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert changed == [1], f"改动扩散到了其它行：{changed}"


class TestOneComplementChecksum:
    def test_a_known_record_is_reproduced_exactly(self):
        """手算一条：count=05, addr=1234, data=0352 → sum=0xA0 → checksum=0x5F。"""
        line = (
            S19Writer()
            .serialize(_image(0x1234, b"\x03\x52"), {})
            .decode("ascii")
            .splitlines()[0]
        )
        assert line == "S105123403525F"

    def test_it_is_not_the_hex_two_complement(self):
        """同地址同数据，两种格式的 checksum 必然不同 —— 抄错就会被抓住。"""
        image = _image(0x1234, b"\x03\x52")
        s19 = S19Writer().serialize(image, {}).decode("ascii").splitlines()[0]
        # HEX 会先发扩展线性地址记录（04），所以按记录类型取数据行
        hex_lines = HexWriter().serialize(image, {}).decode("ascii").splitlines()
        hex_line = next(ln for ln in hex_lines if ln[7:9] == "00")
        assert hex_line == ":02123400035263"  # 二补数：63
        assert s19[-2:] == "5F"  # 一补数：5F
        assert s19[-2:] != hex_line[-2:]

    def test_every_line_satisfies_the_one_complement_rule(self):
        """写回产出的每一行，都必须满足 S-Record 的校验规则。"""
        produced = S19Writer().serialize(_parse(), {"source_bytes": _raw()})
        for line in produced.decode("ascii").splitlines():
            raw = bytes.fromhex(line[2:])
            assert (sum(raw) & 0xFF) == 0xFF, f"行校验失败：{line}"


class TestAddressWidth:
    def test_16_bit_address_uses_s1(self):
        line = (
            S19Writer()
            .serialize(_image(0x1234, b"\x01"), {})
            .decode("ascii")
            .splitlines()[0]
        )
        assert line.startswith("S1")

    def test_24_bit_address_uses_s2(self):
        line = (
            S19Writer()
            .serialize(_image(0x123456, b"\x01"), {})
            .decode("ascii")
            .splitlines()[0]
        )
        assert line.startswith("S2")

    def test_32_bit_address_uses_s3(self):
        line = (
            S19Writer()
            .serialize(_image(0x80001234, b"\x01"), {})
            .decode("ascii")
            .splitlines()[0]
        )
        assert line.startswith("S3")

    def test_a_line_never_crosses_the_address_space_boundary(self):
        """S1 的地址字段只有 16 位：跨 0x10000 必须换类型或拆行，不能截断。"""
        image = _image(0xFFFE, b"\xaa" * 8)
        lines = S19Writer().serialize(image, {}).decode("ascii").splitlines()
        data_lines = [ln for ln in lines if ln[1] in "123"]
        # 0xFFFE 起 8 字节会跨过 0x10000，必须至少拆成两行
        assert len(data_lines) >= 2
        for line in data_lines:
            assert (sum(bytes.fromhex(line[2:])) & 0xFF) == 0xFF


class TestFileLevelDetails:
    def test_header_is_restored_from_the_source(self):
        produced = S19Writer().serialize(_parse(), {"source_bytes": _raw()})
        assert produced.decode("ascii").splitlines()[0].startswith("S0")

    def test_without_a_source_there_is_no_header(self):
        """不知道原文件就不编一个 —— 编出来的头是假的，比没有更糟。"""
        produced = S19Writer().serialize(_parse(), {})
        assert not produced.decode("ascii").splitlines()[0].startswith("S0")

    def test_explicit_header_wins(self):
        """S0 里的数据是十六进制编码的字节 —— 断言要看解码后的内容。"""
        produced = S19Writer().serialize(_parse(), {"header": "HELLO"})
        first = produced.decode("ascii").splitlines()[0]
        assert first.startswith("S0")
        assert bytes.fromhex(first[2:])[3:-1] == b"HELLO"

    def test_terminator_is_s7_by_default(self):
        line = S19Writer().serialize(_parse(), {}).decode("ascii").splitlines()[-1]
        assert line.startswith("S7")

    def test_terminator_type_can_be_chosen(self):
        line = (
            S19Writer()
            .serialize(_parse(), {"terminator": "9"})
            .decode("ascii")
            .splitlines()[-1]
        )
        assert line.startswith("S9")

    def test_line_ending_defaults_to_lf(self):
        assert b"\r" not in S19Writer().serialize(_parse(), {})

    def test_line_ending_is_taken_from_the_source(self):
        produced = S19Writer().serialize(_parse(), {"source_bytes": _raw()})
        assert (b"\r\n" in produced) == (b"\r\n" in _raw())


class TestDefaultSplitting:
    def test_splits_when_the_segment_has_no_boundaries(self):
        """程序构造的段没有记录边界，按上限切 —— 但不得让 count 字段溢出。"""
        image = _image(0x1000, b"\x00" * 100)
        produced = S19Writer().serialize(image, {"max_bytes_per_line": 32})
        lines = [ln for ln in produced.decode("ascii").splitlines() if ln[1] in "123"]
        assert len(lines) == 4  # 32 + 32 + 32 + 4
        for line in lines:
            assert (sum(bytes.fromhex(line[2:])) & 0xFF) == 0xFF

    def test_empty_image_still_has_a_terminator(self):
        """空镜像也要是一个合法文件 —— 至少得有结束记录。"""
        produced = S19Writer().serialize(AddressSpace(segments=[]), {})
        assert produced.decode("ascii").splitlines()[-1].startswith("S7")
