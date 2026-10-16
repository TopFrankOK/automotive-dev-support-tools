"""校验和重算的测试。

**为什么单独测它**：改完标定值不更新校验和，刷进 ECU 会直接被拒。
而「校验和没算」这件事本身**不报任何错** —— 流程跑完，文件写出去了，
到台架上才发现刷不进去。这是标定工具里最难查的一类问题，
因为它看起来每一步都成功了。

所以这里的规则是：**算不了就报错，不许静默跳过、更不许填 0。**
填一个看起来合理的错值，比报错糟糕得多。
"""

from __future__ import annotations

import zlib

import pytest

from automotive_dev_support.core.models.memory import (
    AddressSpace,
    ChecksumError,
    ChecksumKind,
    ChecksumSpec,
    ImageAssembler,
)

DATA_START = 0x1000
DATA_END = 0x1010
STORE = 0x2000


def _space(blob: bytes | None = None) -> AddressSpace:
    asm = ImageAssembler()
    asm.add(DATA_START, bytes(range(16)) if blob is None else blob)
    asm.add(STORE, b"\x00" * 4)  # 存放校验和的地方
    return asm.build()


def _spec(**kwargs: object) -> ChecksumSpec:
    defaults: dict[str, object] = {
        "name": "c",
        "kind": ChecksumKind.SUM32,
        "start": DATA_START,
        "end": DATA_END,
        "store_address": STORE,
    }
    defaults.update(kwargs)
    return ChecksumSpec(**defaults)  # type: ignore[arg-type]


class TestRecompute:
    def test_sum32_value_is_stored(self):
        space = _space()
        space.checksums = [_spec()]
        results = space.recompute_checksums()
        assert results["c"] == sum(
            int.from_bytes(space.read(DATA_START + i, 4), "big") for i in range(0, 16, 4)
        )
        assert space.read(STORE, 4) == results["c"].to_bytes(4, "big")

    def test_changes_when_data_changes(self):
        """改了值，校验和必须跟着变 —— 这正是 P4 存在的理由。"""
        space = _space()
        space.checksums = [_spec()]
        first = space.recompute_checksums()["c"]

        space.write(DATA_START, b"\xff\xff\xff\xff")
        second = space.recompute_checksums()["c"]
        assert second != first
        assert space.read(STORE, 4) == second.to_bytes(4, "big")

    def test_crc32_matches_known_value(self):
        """对得上公开校验值，说明算法没写反（字节序、初值）。"""
        space = _space(b"123456789")
        spec = _spec(kind=ChecksumKind.CRC32, end=DATA_START + 9)
        space.checksums = [spec]
        assert space.recompute_checksums()["c"] == 0xCBF43926
        assert zlib.crc32(b"123456789") == 0xCBF43926

    def test_sum8(self):
        space = _space(b"\x01\x02\x03")
        space.checksums = [_spec(kind=ChecksumKind.SUM8, end=DATA_START + 3)]
        assert space.recompute_checksums()["c"] == 6

    def test_xor8(self):
        space = _space(b"\x01\x02\x03")
        space.checksums = [_spec(kind=ChecksumKind.XOR8, end=DATA_START + 3)]
        assert space.recompute_checksums()["c"] == 0x01 ^ 0x02 ^ 0x03

    def test_init_and_xor_out_are_applied(self):
        space = _space(b"\x01\x02\x03")
        space.checksums = [
            _spec(kind=ChecksumKind.SUM8, end=DATA_START + 3, init_value=1, xor_out=0xFF)
        ]
        assert space.recompute_checksums()["c"] == ((6 + 1) & 0xFF) ^ 0xFF

    def test_no_rules_returns_empty(self):
        """没有规则就不算 —— 这是正常的，不是错误。"""
        assert _space().recompute_checksums() == {}

    def test_kind_none_is_skipped(self):
        space = _space()
        space.checksums = [_spec(kind=ChecksumKind.NONE)]
        assert space.recompute_checksums() == {}
        assert space.read(STORE, 4) == b"\x00\x00\x00\x00"


class TestRefusesToGuess:
    def test_region_not_covered(self):
        """被校验区间里有空洞 → 算出来是假的，必须报。"""
        space = _space()
        space.checksums = [_spec(start=0x8000, end=0x8010)]
        with pytest.raises(ChecksumError, match="不在镜像内"):
            space.recompute_checksums()

    def test_store_address_not_covered(self):
        space = _space()
        space.checksums = [_spec(store_address=0x9000)]
        with pytest.raises(ChecksumError, match="存放地址"):
            space.recompute_checksums()

    def test_no_store_address(self):
        space = _space()
        space.checksums = [_spec(store_address=None)]
        with pytest.raises(ChecksumError, match="存放地址"):
            space.recompute_checksums()

    def test_vendor_algorithm_is_refused(self):
        """厂商私有算法：算错一个填进去，比报错糟糕得多。"""
        space = _space()
        space.checksums = [_spec(algorithm_ref="VENDOR_CRC")]
        with pytest.raises(ChecksumError, match="私有算法"):
            space.recompute_checksums()

    def test_unknown_kind_is_refused(self):
        """不许像从前那样 `return 0` 糊过去。"""
        from automotive_dev_support.core.models.memory.model import _compute_checksum

        spec = _spec().model_copy(update={"kind": "some_new_kind"})
        with pytest.raises(ChecksumError):
            _compute_checksum(b"\x01\x02", spec)

    def test_nothing_is_written_when_it_fails(self):
        """算不出来就不能留下半截：存放地址必须还是原样。"""
        space = _space()
        space.checksums = [_spec(start=0x8000, end=0x8010)]
        with pytest.raises(ChecksumError):
            space.recompute_checksums()
        assert space.read(STORE, 4) == b"\x00\x00\x00\x00"
