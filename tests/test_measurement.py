"""测量域核心的测试：模型形状、解码、抽稀、对齐。

这一层全是纯计算，测试的价值在于把「语义决定」钉死 ——
比如 min/max 抽稀必须保住尖峰、公共窗无交集必须返回 None。
这些行为一旦被「优化」悄悄改掉，趋势视图就会开始撒谎。
"""

from __future__ import annotations

import math
import struct
from array import array

import pytest

from automotive_dev_support.core.models.measurement import (
    ChannelGroup,
    DataLocation,
    DataTypeSpec,
    Event,
    Measurement,
    SignalKind,
    SignalSeries,
    ValueDtype,
)
from automotive_dev_support.core.models.measurement.align import (
    common_window,
    make_grid,
    resample,
    shift,
)
from automotive_dev_support.core.models.measurement.decode import decode_samples
from automotive_dev_support.core.models.measurement.decimate import bucketize, minmax_bands


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------


class TestDecodeSamples:
    def test_fast_path_when_stride_equals_size(self):
        # 单通道记录：步长 == 元素大小，走整块转换
        spec = DataTypeSpec(dtype=ValueDtype.UINT, bit_count=16, byte_order="little")
        raw = (1000).to_bytes(2, "little") + (2000).to_bytes(2, "little")
        out = decode_samples(raw, spec, count=2, stride=2, offset=0)
        assert out is not None
        assert list(out) == [1000, 2000]

    def test_strided_extraction_picks_channel_offset(self):
        # 记录 = [uint16 A][uint16 B]，抽 B
        spec = DataTypeSpec(dtype=ValueDtype.UINT, bit_count=16)
        raw = b"".join(
            (a).to_bytes(2, "little") + (a * 10).to_bytes(2, "little")
            for a in (1, 2, 3)
        )
        out = decode_samples(raw, spec, count=3, stride=4, offset=2)
        assert list(out) == [10, 20, 30]

    def test_big_endian_is_byteswapped(self):
        spec = DataTypeSpec(dtype=ValueDtype.UINT, bit_count=32, byte_order="big")
        raw = (0x01020304).to_bytes(4, "big")
        out = decode_samples(raw, spec, count=1, stride=4, offset=0)
        assert list(out) == [0x01020304]

    def test_signed_and_float(self):
        signed = DataTypeSpec(dtype=ValueDtype.INT, bit_count=16)
        assert list(decode_samples((-5).to_bytes(2, "little", signed=True), signed, 1, 2, 0)) == [-5]
        real = DataTypeSpec(dtype=ValueDtype.FLOAT, bit_count=64)
        raw = struct.pack("<d", math.pi)
        out = decode_samples(raw, real, 1, 8, 0)
        assert out[0] == pytest.approx(math.pi)

    def test_bit_packed_returns_none(self):
        # 位压缩：宁可不解，不可解错
        spec = DataTypeSpec(dtype=ValueDtype.UINT, bit_count=12)
        assert decode_samples(b"\x01\x02", spec, 1, 2, 0) is None

    def test_zero_count_returns_empty(self):
        spec = DataTypeSpec(dtype=ValueDtype.UINT, bit_count=16)
        out = decode_samples(b"", spec, 0, 2, 0)
        assert list(out) == []

    def test_truncated_data_returns_none(self):
        spec = DataTypeSpec(dtype=ValueDtype.UINT, bit_count=16)
        # 声明 4 个样本但只有 3 个的字节
        raw = b"\x01\x00" * 3
        assert decode_samples(raw, spec, 4, 2, 0) is None


# ---------------------------------------------------------------------------
# 抽稀
# ---------------------------------------------------------------------------


class TestMinMaxBands:
    def test_short_series_passes_through_per_point(self):
        times = array("d", [0.0, 1.0, 2.0])
        values = array("d", [5.0, 7.0, 6.0])
        bands = minmax_bands(times, values, max_points=10)
        assert len(bands) == 3
        assert bands[1].v_min == bands[1].v_max == 7.0

    def test_band_count_is_capped(self):
        n = 10_000
        times = array("d", (i * 0.001 for i in range(n)))
        values = array("d", (float(i % 100) for i in range(n)))
        for cap in (10, 137, 1000):
            bands = minmax_bands(times, values, max_points=cap)
            assert len(bands) <= cap

    def test_spike_survives_as_band_height(self):
        # 等距抽样会丢掉的尖峰，min/max 频带必须留下痕迹
        values = array("d", [1.0] * 99 + [900.0] + [1.0])
        times = array("d", (i * 0.01 for i in range(101)))
        bands = minmax_bands(times, values, max_points=10)
        tallest = max(bands, key=lambda b: b.v_max)
        assert tallest.v_max == 900.0
        assert tallest.v_min == 1.0

    def test_empty_input(self):
        assert minmax_bands(array("d"), array("d"), 10) == []


class TestBucketize:
    def test_mean_buckets_are_time_aligned(self):
        times = array("d", [0.0, 0.5, 1.0, 1.5])
        values = array("d", [1.0, 3.0, 2.0, 4.0])
        out_t, out_v = bucketize(times, values, bucket_s=1.0, stat="mean")
        assert list(out_t) == [0.0, 1.0]
        assert list(out_v) == [2.0, 3.0]

    def test_max_and_min(self):
        times = array("d", [0.0, 0.5, 1.0, 1.5])
        values = array("d", [1.0, 3.0, 2.0, 4.0])
        assert list(bucketize(times, values, 1.0, "max")[1]) == [3.0, 4.0]
        assert list(bucketize(times, values, 1.0, "min")[1]) == [1.0, 2.0]

    def test_bucket_boundaries_match_across_series(self):
        # 同一起点、同一桶宽 → 桶边界一致，结果才可直接相减
        a_t = array("d", [0.0, 0.3, 1.1])
        b_t = array("d", [0.0, 0.9, 1.9])
        t1, _ = bucketize(a_t, array("d", [1.0, 1.0, 1.0]), 1.0)
        t2, _ = bucketize(b_t, array("d", [1.0, 1.0, 1.0]), 1.0)
        assert list(t1) == list(t2)

    def test_non_positive_bucket_raises(self):
        with pytest.raises(ValueError):
            bucketize(array("d", [0.0]), array("d", [1.0]), 0.0)


# ---------------------------------------------------------------------------
# 对齐
# ---------------------------------------------------------------------------


class TestCommonWindow:
    def test_intersection(self):
        assert common_window([(0.0, 10.0), (5.0, 20.0)]) == (5.0, 10.0)

    def test_no_overlap_returns_none(self):
        # 时间上不重叠的两段录音：任何「对齐后比较」都是假的
        assert common_window([(0.0, 5.0), (6.0, 10.0)]) is None

    def test_ignores_empty_and_none(self):
        assert common_window([None, (3.0, 4.0), (0.0, 0.0)]) == (3.0, 4.0)

    def test_empty_list(self):
        assert common_window([]) is None


class TestResample:
    def test_linear_interpolation(self):
        times = array("d", [0.0, 1.0])
        values = array("d", [0.0, 10.0])
        grid = array("d", [0.0, 0.25, 0.5, 1.0])
        out = resample(times, values, grid)
        assert [pytest.approx(v) for v in out] == [0.0, 2.5, 5.0, 10.0]

    def test_clamps_outside_range(self):
        # 截断不外推：外推出来的趋势是编造的
        times = array("d", [1.0, 2.0])
        values = array("d", [5.0, 7.0])
        out = resample(times, values, array("d", [0.0, 3.0]))
        assert list(out) == [5.0, 7.0]

    def test_single_point(self):
        out = resample(array("d", [1.0]), array("d", [42.0]), array("d", [0.0, 9.0]))
        assert list(out) == [42.0, 42.0]


class TestMakeGrid:
    def test_inclusive_endpoint(self):
        grid = make_grid(0.0, 1.0, 0.1)
        assert len(grid) == 11
        assert grid[-1] == pytest.approx(1.0)

    def test_non_positive_step_raises(self):
        with pytest.raises(ValueError):
            make_grid(0.0, 1.0, 0.0)


class TestShift:
    def test_returns_new_series(self):
        times = array("d", [0.0, 1.0])
        values = array("d", [3.0, 4.0])
        t2, v2 = shift(times, values, 2.5)
        assert list(t2) == [2.5, 3.5]
        assert list(times) == [0.0, 1.0]  # 输入未被改动


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


def _group_with_master(ns_values: list[int]) -> ChannelGroup:
    master = SignalSeries(
        path="time", kind=SignalKind.CONTINUOUS, values=array("q", ns_values)
    )
    return ChannelGroup(index=0, name="g0", master=master, signals=[])


class TestChannelGroup:
    def test_seconds_are_relative_to_first_sample(self):
        grp = _group_with_master([1_000_000_000, 1_500_000_000, 3_000_000_000])
        secs = grp.seconds()
        assert list(secs) == [0.0, 0.5, 2.0]

    def test_seconds_without_master(self):
        assert ChannelGroup(index=0).seconds() is None

    def test_signal_lookup_by_path_or_name(self):
        grp = ChannelGroup(
            index=0,
            signals=[
                SignalSeries(path="Eng.Speed", name="n"),
                SignalSeries(path="Temp", name="Coolant"),
            ],
        )
        assert grp.signal("Eng.Speed") is not None
        assert grp.signal("Coolant") is not None
        assert grp.signal("Nope") is None


class TestMeasurement:
    def _measurement(self) -> Measurement:
        grp = _group_with_master([0, 1_000_000_000, 2_000_000_000])
        grp.signals.append(
            SignalSeries(
                path="EngSpeed",
                unit="rpm",
                group=0,
                values=array("d", [800.0, 820.0, 900.0]),
            )
        )
        return Measurement(label="run1", groups=[grp])

    def test_signals_flatten_including_master(self):
        m = self._measurement()
        paths = [s.path for s in m.signals()]
        assert paths == ["time", "EngSpeed"]

    def test_signal_and_counts(self):
        m = self._measurement()
        assert m.signal("EngSpeed").unit == "rpm"
        assert m.signal_count() == 2
        assert m.loaded_count() == 2

    def test_time_range_and_duration(self):
        m = self._measurement()
        assert m.time_range() == (0.0, 2.0)
        assert m.duration() == 2.0

    def test_events_by_id_and_counts(self):
        m = Measurement(
            events=[
                Event(t=0.1, channel=1, frame_id=0x850, data=b"\x01"),
                Event(t=0.2, channel=1, frame_id=0x850, data=b"\x02"),
                Event(t=0.3, channel=2, frame_id=0x100, data=b"\x03"),
            ]
        )
        grouped = m.events_by_id()
        assert len(grouped[(1, 0x850)]) == 2
        assert m.frame_counts() == {(1, 0x850): 2, (2, 0x100): 1}
        assert m.time_range() == (0.1, 0.3)

    def test_empty_measurement_time_range_is_none(self):
        assert Measurement().time_range() is None

    def test_lazy_location_carries_layout(self):
        # 索引阶段产出 location，不产 values —— 这就是「不整体载入」的落点
        loc = DataLocation(
            uri="file:///x.mf4",
            start=4096,
            stride=24,
            offset=8,
            count=1000,
            spec=DataTypeSpec(dtype=ValueDtype.UINT, bit_count=32),
        )
        series = SignalSeries(path="Speed", location=loc)
        assert series.sample_count == 1000
        assert not series.loaded
        assert loc.span == 24_000
