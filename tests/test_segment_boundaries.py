"""记录边界的测试。

**为什么有这个东西**：解析 HEX / S19 时，连续记录会被合并成一段。
合并之后，原来「一行到哪里结束」的信息就没了。
写回时只能按默认字节数重新切行 —— 结果未改动的行也变了，
`git diff` 显示整文件改动，**改了哪个量就看不出来了**。

标定文件是要进版本库、要过评审的。所以边界必须留下来。

边界的语义是格式无关的：「这段由哪些连续记录拼成」，
所以它可以（也应该）放在 core 里，而不是塞进某个解析器插件。
"""

from __future__ import annotations

from automotive_dev_support.core.models.memory import ImageAssembler, Segment


class TestContiguousChunks:
    def test_two_chunks_keep_the_seam(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x110, b"\xbb" * 16)
        space = asm.build()
        assert len(space.segments) == 1
        assert space.segments[0].boundaries == (0, 16)

    def test_three_chunks_keep_every_seam(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x110, b"\xbb" * 16)
        asm.add(0x120, b"\xcc" * 16)
        space = asm.build()
        assert space.segments[0].boundaries == (0, 16, 32)

    def test_data_is_unchanged_by_recording_boundaries(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x110, b"\xbb" * 16)
        space = asm.build()
        assert space.segments[0].data == b"\xaa" * 16 + b"\xbb" * 16


class TestDisjointChunks:
    def test_gap_makes_two_segments(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x200, b"\xbb" * 16)
        space = asm.build()
        assert len(space.segments) == 2
        assert [s.boundaries for s in space.segments] == [(0,), (0,)]

    def test_gap_is_not_filled(self):
        """空洞不许被填 —— 写回不能发明不存在的字节。"""
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x200, b"\xbb" * 16)
        space = asm.build()
        assert [s.start for s in space.segments] == [0x100, 0x200]


class TestSingleChunk:
    def test_is_not_split(self):
        """单个记录不许被自动切成 16 字节一块 —— 那会改变原文件布局。"""
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 40)
        space = asm.build()
        assert space.segments[0].boundaries == (0,)

    def test_short_chunk(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa")
        assert asm.build().segments[0].boundaries == (0,)


class TestOverlap:
    """重叠的文件本身就是坏的，装配器的处理是「后者覆盖前者」+ 告警。

    这里的断言只关心**边界的不变量**：起点为 0、单调递增。
    至于重叠区里每个字节最终是什么，那是「后者覆盖前者」的定义问题，
    不在这里钉死 —— 钉死了反而会掩盖「这类文件本来就不该出现」这个事实。
    """

    def test_overlap_adds_a_seam_at_the_overwrite_point(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x108, b"\xcc" * 8)
        space = asm.build()
        assert space.segments[0].boundaries == (0, 8)

    def test_rewrite_from_the_very_start_keeps_one_seam(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x100, b"\xbb" * 16)
        space = asm.build()
        assert space.segments[0].boundaries == (0,)
        assert space.segments[0].data == b"\xbb" * 16

    def test_overlap_is_reported(self):
        """重叠必须被看见 —— 「以为加载的是 A 其实加载的是 B」是数据事故。"""
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x108, b"\xcc" * 8)
        asm.build()
        assert any("重叠" in w for w in asm.warnings)

    def test_overwrite_takes_effect(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x108, b"\xcc" * 8)
        data = asm.build().segments[0].data
        assert data[:8] == b"\xaa" * 8
        assert data[8:10] == b"\xcc" * 2

    def test_out_of_order_overlap_keeps_marks_sane(self):
        """乱序文件（HEX 允许）：边界仍须从 0 开始且单调递增。"""
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x110, b"\xbb" * 16)
        asm.add(0x108, b"\xcc" * 16)
        marks = asm.build().segments[0].boundaries
        assert marks[0] == 0
        assert list(marks) == sorted(marks)


class TestEdgeCases:
    def test_empty_assembler(self):
        assert ImageAssembler().build().segments == []

    def test_empty_data_is_ignored(self):
        asm = ImageAssembler()
        asm.add(0x100, b"")
        assert asm.build().segments == []

    def test_negative_address_is_skipped(self):
        asm = ImageAssembler()
        asm.add(-1, b"\xaa")
        assert asm.build().segments == []
        assert asm.warnings

    def test_repeated_build_is_stable(self):
        asm = ImageAssembler()
        asm.add(0x100, b"\xaa" * 16)
        asm.add(0x110, b"\xbb" * 16)
        first = asm.build()
        second = asm.build()
        assert [s.boundaries for s in first.segments] == [
            s.boundaries for s in second.segments
        ]
        assert first.segments[0].data == second.segments[0].data

    def test_boundaries_default_to_empty(self):
        """直接构造的段（没有经过装配器）没有边界信息。"""
        assert Segment(start=0x100, data=b"\xaa").boundaries == ()

    def test_boundaries_survive_serialization(self):
        seg = Segment(start=0x100, data=b"\xaa" * 8, boundaries=(0, 4))
        restored = Segment.model_validate_json(seg.model_dump_json())
        assert restored.boundaries == (0, 4)
