"""测量分析用例测试：trend 抽稀 / 时间窗 / 事件流速率 / 对齐。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from automotive_dev_support.core.models.measurement import SignalKind
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.services import (
    align_signal,
    extract_trend,
    format_event_key,
    load_measurement,
)

SAMPLE_DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load_all([Path("src/automotive_dev_support/plugins")])
    return reg


@pytest.fixture(scope="module")
def v1(registry) -> object:
    return load_measurement(registry, SAMPLE_DATA / "sample_v1.mf4")


@pytest.fixture(scope="module")
def v2(registry) -> object:
    return load_measurement(registry, SAMPLE_DATA / "sample_v2.mf4")


class TestLoadMeasurement:
    def test_loads_mf4_without_warnings(self, v1):
        assert v1.warnings == []
        assert len(v1.document.groups) == 3
        assert v1.document.label == "sample_v1"

    def test_rejects_unknown_format(self, registry, tmp_path):
        bogus = tmp_path / "x.xyz"
        bogus.write_bytes(b"nonsense")
        with pytest.raises(ValueError, match="没有能处理"):
            load_measurement(registry, bogus)

    def test_rejects_non_measurement_document(self, registry, tmp_path):
        # A2L 能解析，但产物不是 Measurement
        src = SAMPLE_DATA / "sample_v1.a2l"
        if not src.exists():
            pytest.skip("A2L 样例不在")
        with pytest.raises(TypeError, match="不是测量数据"):
            load_measurement(registry, src)


class TestTrend:
    def test_continuous_signal_decimates_to_bands(self, v1):
        result = extract_trend(v1, "EngineSpeed", max_points=10)
        assert result.kind == SignalKind.CONTINUOUS
        assert result.unit == "rpm"
        assert result.sample_count == 100
        assert result.bands is not None
        assert len(result.bands) == 10  # 100 样本 → 10 带
        # 每个带都盖住一段时间，且 min <= max
        for band in result.bands:
            assert band.v_min <= band.v_max
            assert band.t_start <= band.t_end
        # 统计覆盖全量（与生成公式同源，不用近似猜测）
        expected = [800.0 + 100.0 * math.sin(i / 10.0) for i in range(100)]
        assert result.stats["min"] == pytest.approx(min(expected))
        assert result.stats["max"] == pytest.approx(max(expected))
        assert result.stats["mean"] == pytest.approx(math.fsum(expected) / 100)

    def test_minmax_decimation_keeps_spike(self, v2):
        """v2 第 50 个样本有 +200 尖峰 —— 等距抽样会丢，min/max 不会。"""
        result = extract_trend(v2, "EngineSpeed", max_points=10)
        highs = [band.v_max for band in result.bands]
        expected = [
            800.0 + 100.0 * math.sin(i / 10.0) + 50.0 + (200.0 if i == 50 else 0.0)
            for i in range(100)
        ]
        assert max(highs) == pytest.approx(max(expected))

    def test_window_clips_samples(self, v1):
        full = extract_trend(v1, "EngineSpeed", max_points=1000)
        clipped = extract_trend(v1, "EngineSpeed", max_points=1000, window=(0.2, 0.5))
        assert clipped.sample_count < full.sample_count
        assert clipped.window[0] == pytest.approx(0.2)
        assert clipped.window[1] == pytest.approx(0.5)
        assert all(0.2 <= t <= 0.5 for t in [b.t_start for b in clipped.bands])

    def test_unknown_signal_raises(self, v1):
        with pytest.raises(KeyError, match="找不到信号"):
            extract_trend(v1, "NoSuchSignal")

    def test_master_channel_is_findable(self, v1):
        result = extract_trend(v1, "time", max_points=50)
        assert result.sample_count == 100


class TestEventTrend:
    def test_event_key_produces_rate_series(self, registry):
        loaded = load_measurement(registry, SAMPLE_DATA / "sample_v1.asc")
        result = extract_trend(loaded, "ch1/0x850", max_points=20)
        assert result.kind == SignalKind.EVENT
        assert result.unit == "frames/s"
        assert result.sample_count == 3  # 样例里 0x850 有三帧
        assert result.times is not None and len(result.times) > 0
        # 桶内帧数之和 = 总帧数
        assert sum(result.values) == pytest.approx(3.0)
        assert result.stats["mean_rate"] > 0

    def test_event_key_decimal_id_also_works(self, registry):
        loaded = load_measurement(registry, SAMPLE_DATA / "sample_v1.asc")
        result = extract_trend(loaded, "ch1/2128", max_points=20)  # 0x850 的十进制
        assert result.sample_count == 3

    def test_unknown_event_key_raises(self, registry):
        loaded = load_measurement(registry, SAMPLE_DATA / "sample_v1.asc")
        with pytest.raises(KeyError, match="事件流里没有"):
            extract_trend(loaded, "ch9/0x999")

    def test_event_key_format_roundtrip(self):
        assert format_event_key(1, 0x850) == "ch1/0x850"


class TestAlign:
    def test_align_same_signal_across_versions(self, v1, v2):
        result = align_signal(v1, v2, "EngineSpeed", step=0.01)
        assert result.window[0] == pytest.approx(0.0)
        assert result.window[1] == pytest.approx(0.99)
        assert len(result.grid) == 100
        # v2 整体 +50，第 50 样本另有 +200 尖峰
        assert result.diff_max == pytest.approx(250.0, abs=0.5)
        assert result.diff_rms > 50.0

    def test_align_with_explicit_window(self, v1, v2):
        result = align_signal(v1, v2, "EngineSpeed", step=0.1, window=(0.0, 0.5))
        assert result.window == (0.0, 0.5)
        assert len(result.grid) == 6

    def test_align_missing_signal_raises(self, v1, v2):
        with pytest.raises(KeyError, match="找不到信号"):
            align_signal(v1, v2, "NoSuchSignal")

    def test_align_disjoint_windows_raises(self, v1, v2):
        with pytest.raises(ValueError, match="没有交集"):
            align_signal(v1, v2, "EngineSpeed", step=0.01, window=(5.0, 6.0))

    def test_align_identical_gives_zero_diff(self, v1):
        result = align_signal(v1, v1, "CoolantTemp", step=0.05)
        assert result.diff_max == pytest.approx(0.0)
        assert result.diff_rms == pytest.approx(0.0)
