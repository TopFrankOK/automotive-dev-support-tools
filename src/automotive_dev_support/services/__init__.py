"""应用层：用例编排。只允许依赖 core 与 platform。"""

from __future__ import annotations

from automotive_dev_support.services.compare import (
    CalibrationSources,
    CompareOutcome,
    LoadedCalibration,
    load_calibration,
    run_compare,
)
from automotive_dev_support.services.analyze import (
    AlignedSignal,
    LoadedMeasurement,
    TrendResult,
    align_signal,
    extract_trend,
    format_event_key,
    load_measurement,
)
from automotive_dev_support.services.measurement import (
    ensure_time_axis,
    load_group,
    load_series,
    read_records,
)

__all__ = [
    "CalibrationSources",
    "CompareOutcome",
    "LoadedCalibration",
    "load_calibration",
    "run_compare",
    "AlignedSignal",
    "LoadedMeasurement",
    "TrendResult",
    "align_signal",
    "extract_trend",
    "format_event_key",
    "load_measurement",
    "ensure_time_axis",
    "load_group",
    "load_series",
    "read_records",
]
