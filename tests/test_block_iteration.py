"""按边界产出块的测试。

writer 插件需要一份「切好的 (地址, 字节) 块」才能编码文本行。
切分本身是**格式无关**的（哪个地址到哪个地址算一块），
所以它在 core 里；插件只负责把块变成自己那种格式的一行文本。

这里最要紧的一条不变量是：

> **所有块拼起来必须恰好等于原段的字节，不重叠、不遗漏、不发明。**

写回要能重建完整镜像；少一个字节就是静默丢数据。
"""

from __future__ import annotations

from automotive_dev_support.core.models.memory import AddressSpace, Segment


def _space(*segments: Segment) -> AddressSpace:
    return AddressSpace(segments=list(segments))


class TestWithBoundaries:
    def test_blocks_follow_the_record_layout(self):
        seg = Segment(
            start=0x100, data=b"\xaa" * 16 + b"\xbb" * 16, boundaries=(0, 16)
        )
        blocks = list(_space(seg).iter_blocks())
        assert blocks == [(0x100, b"\xaa" * 16), (0x110, b"\xbb" * 16)]

    def test_three_boundaries_make_three_blocks(self):
        data = b"\xaa" * 8 + b"\xbb" * 8 + b"\xcc" * 8
        seg = Segment(start=0x100, data=data, boundaries=(0, 8, 16))
        blocks = list(_space(seg).iter_blocks())
        assert [b for _, b in blocks] == [b"\xaa" * 8, b"\xbb" * 8, b"\xcc" * 8]

    def test_boundaries_are_not_re_split(self):
        """原文件的布局优先：不因为 max_bytes 更小就把行再切开。

        按格式上限（如 HEX 每行 ≤0xFF）再切是 writer 的事 ——
        那是格式知识，不该由 core 决定。
        """
        seg = Segment(start=0x100, data=b"\xaa" * 8, boundaries=(0, 4))
        blocks = list(_space(seg).iter_blocks(max_bytes=2))
        assert [len(b) for _, b in blocks] == [4, 4]


class TestWithoutBoundaries:
    def test_max_bytes_splits_evenly(self):
        seg = Segment(start=0x100, data=b"\xaa" * 10)
        blocks = list(_space(seg).iter_blocks(max_bytes=4))
        assert blocks == [
            (0x100, b"\xaa" * 4),
            (0x104, b"\xaa" * 4),
            (0x108, b"\xaa" * 2),
        ]

    def test_last_block_may_be_short(self):
        seg = Segment(start=0x100, data=b"\xaa" * 5)
        blocks = list(_space(seg).iter_blocks(max_bytes=4))
        assert [len(b) for _, b in blocks] == [4, 1]

    def test_without_max_bytes_the_whole_segment_is_one_block(self):
        seg = Segment(start=0x100, data=b"\xaa" * 40)
        blocks = list(_space(seg).iter_blocks())
        assert blocks == [(0x100, b"\xaa" * 40)]


class TestInvariants:
    def test_blocks_reconstruct_the_segment_exactly(self):
        seg = Segment(
            start=0x100, data=bytes(range(48)), boundaries=(0, 16, 32)
        )
        blocks = list(_space(seg).iter_blocks())
        rebuilt = b"".join(b for _, b in blocks)
        assert rebuilt == seg.data
        assert blocks[0][0] == seg.start
        for (addr, blob), next_block in zip(blocks, blocks[1:]):
            assert addr + len(blob) == next_block[0], "块必须首尾相接"

    def test_addresses_are_monotonic(self):
        seg_a = Segment(start=0x200, data=b"\xaa" * 8, boundaries=(0, 4))
        seg_b = Segment(start=0x100, data=b"\xbb" * 8, boundaries=(0, 4))
        blocks = list(_space(seg_a, seg_b).iter_blocks())
        addresses = [a for a, _ in blocks]
        assert addresses == sorted(addresses)

    def test_gaps_produce_no_blocks(self):
        """空洞不许产出块 —— 写回不能发明不存在的字节。"""
        seg_a = Segment(start=0x100, data=b"\xaa" * 8, boundaries=(0,))
        seg_b = Segment(start=0x300, data=b"\xbb" * 8, boundaries=(0,))
        blocks = list(_space(seg_a, seg_b).iter_blocks())
        assert [a for a, _ in blocks] == [0x100, 0x300]

    def test_empty_segment_yields_nothing(self):
        seg = Segment(start=0x100, data=b"", boundaries=(0,))
        assert list(_space(seg).iter_blocks()) == []

    def test_empty_space_yields_nothing(self):
        assert list(AddressSpace().iter_blocks()) == []

    def test_boundaries_missing_first_mark_is_tolerated(self):
        """边界不是从 0 开始（数据被截断过）也要能切，不能静默丢前缀。"""
        seg = Segment(start=0x100, data=b"\xaa" * 8, boundaries=(4,))
        blocks = list(_space(seg).iter_blocks())
        assert b"".join(b for _, b in blocks) == seg.data
        assert blocks[0][0] == 0x100
