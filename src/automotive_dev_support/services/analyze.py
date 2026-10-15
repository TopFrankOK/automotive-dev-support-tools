"""测量数据分析用例 —— 应用层。

两个入口场景：

    trend   一条信号抽稀后的序列（给趋势视图）
    align   两条信号对齐到公共栅格（给「两次测量差多少」）

与 `compare.py` 同构：CLI / API / 桌面壳都走这里，
入口层不碰插件。文件解析经注册表按后缀选解析器 ——
新增一种测量格式（TRC、PCAP）时本文件一行不改。

## 事件流的「趋势」是什么

CAN 日志没有连续值，它的趋势是**报文速率**：按时间桶统计
每个 (通道, ID) 的帧数。桶宽由时间跨度与目标点数决定，
所以 `--points` 对连续信号和事件流是同一个语义。
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from automotive_dev_support.core.models.measurement import (
    ChannelGroup,
    Event,
    Measurement,
    MinMaxBand,
    SignalKind,
    SignalSeries,
    bucketize,
    common_window,
    make_grid,
    minmax_bands,
    resample,
)
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, DataSource, JobContext
from automotive_dev_support.platform.store import FileSource, kind_from_suffix
from automotive_dev_support.services.measurement import ensure_time_axis, load_series

__all__ = [
    "LoadedMeasurement",
    "TrendResult",
    "AlignedSignal",
    "load_measurement",
    "extract_trend",
    "align_signal",
    "event_timeline",
]


@dataclass
class LoadedMeasurement:
    document: Measurement
    source: DataSource
    warnings: list[str] = field(default_factory=list)


def load_measurement(registry: PluginRegistry, path: Path) -> LoadedMeasurement:
    """按后缀选解析器，解析一个测量文件。"""
    kind = kind_from_suffix(path)
    parsers = registry.parsers_for(kind)
    if not parsers:
        raise ValueError(f"没有能处理 {kind.value} 格式的解析器插件（文件：{path}）")
    source = FileSource(path)
    outcome = parsers[0].instance.parse(source, JobContext(job_id="analyze"))
    if not isinstance(outcome.document, Measurement):
        raise TypeError(
            f"{path} 解析出的不是测量数据（得到 {type(outcome.document).__name__}）"
        )
    return LoadedMeasurement(
        document=outcome.document, source=source, warnings=list(outcome.warnings)
    )


@dataclass
class TrendResult:
    """一条抽稀后的序列。bands 为 None 表示是事件流（用 counts 语义）。"""

    label: str
    signal: str
    unit: str
    kind: SignalKind
    sample_count: int
    window: tuple[float, float] | None
    bands: list[MinMaxBand] | None = None
    times: array | None = None
    values: array | None = None
    stats: dict[str, float] = field(default_factory=dict)
    note: str = ""
    time_base: str = "relative"
    """"relative"（连续型：相对首样本的秒）或 "absolute"（事件流：Unix 纪元秒）。"""


def _clip(
    times: array, values: array, window: tuple[float, float] | None
) -> tuple[array, array]:
    """按时间窗裁剪（含端点）。window 为 None 时原样返回。"""
    if window is None:
        return times, values
    lo, hi = window
    out_t = array("d")
    out_v = array("d")
    for t, v in zip(times, values):
        if lo <= t <= hi:
            out_t.append(t)
            out_v.append(v)
    return out_t, out_v


def _stats(values) -> dict[str, float]:
    if not len(values):
        return {}
    return {
        "min": float(min(values)),
        "max": float(max(values)),
        "mean": float(math.fsum(values) / len(values)),
        "first": float(values[0]),
        "last": float(values[-1]),
    }


def extract_trend(
    loaded: LoadedMeasurement,
    signal: str,
    *,
    max_points: int = 2000,
    window: tuple[float, float] | None = None,
    group_index: int | None = None,
) -> TrendResult:
    """抽取一条信号的趋势（抽稀后）。

    `signal` 连续型匹配信号名；事件流匹配 `ch<通道>/<ID十六进制>`
    （如 `ch1/0x850`），产出报文速率序列。
    `window` 是相对首样本的秒数区间，None 表示全取。
    """
    m = loaded.document
    series = _find_signal(m, signal, group_index)
    if series is not None:
        if series.kind == SignalKind.EVENT or series.events is not None:
            return _event_trend(loaded, signal, max_points=max_points, window=window)
    elif m.events or m.event_stats:
        # 连续组里没有这个名字 —— 事件流（ASC/BLF）按 键 试
        return _event_trend(loaded, signal, max_points=max_points, window=window)
    else:
        raise KeyError(f"测量里找不到信号 {signal!r}")

    grp = _group_of(m, series)
    if grp is None:
        raise KeyError(f"信号 {signal!r} 不属于任何通道组")
    times = ensure_time_axis(loaded.source, grp)
    if times is None:
        raise ValueError(f"信号 {signal!r} 所在组没有可用的时间主通道")
    values = load_series(loaded.source, series)
    if values is None:
        raise ValueError(f"信号 {signal!r} 取不出值（位宽不支持或数据区损坏）")

    times, values = _clip(times, values, window)
    bands = minmax_bands(times, values, max_points)
    span = (times[0], times[-1]) if len(times) else None
    return TrendResult(
        label=m.label,
        signal=series.name or series.path,
        unit=series.unit,
        kind=SignalKind.CONTINUOUS,
        sample_count=len(values),
        window=span,
        bands=bands,
        stats=_stats(values),
        note=f"{len(values)} → {len(bands)} 带（min/max 抽稀）",
    )


def _event_trend(
    loaded: LoadedMeasurement,
    key: str,
    *,
    max_points: int,
    window: tuple[float, float] | None,
) -> TrendResult:
    """事件流的趋势 = 报文速率（每时间桶帧数）。"""
    m = loaded.document
    channel, frame_id = _parse_event_key(key)
    events = [
        ev
        for ev in m.events
        if ev.channel == channel and ev.frame_id == frame_id
    ]
    if not events and m.event_stats:
        stat = m.event_stats.get((channel, frame_id))
        if stat is None:
            raise KeyError(f"事件流里没有 {key!r}")
        raise ValueError(
            f"{key!r} 只有全量统计（{stat.count} 帧），帧体未保留，"
            f"无法按时间分桶（提高解析器的 max_events 或换小文件）"
        )
    if not events:
        raise KeyError(f"事件流里没有 {key!r}")

    times = array("d", (ev.t for ev in events))
    ones = array("d", (1.0 for _ in events))
    times, ones = _clip(times, ones, window)
    if not len(times):
        raise ValueError(f"{key!r} 在指定时间窗内没有帧")
    span_s = times[-1] - times[0]
    if span_s <= 0:
        span_s = 1.0
    bucket_s = max(span_s / max_points, 1e-9)
    bucket_t, bucket_v = bucketize(times, ones, bucket_s, stat="sum")
    return TrendResult(
        label=m.label,
        signal=key,
        unit="frames/s",
        kind=SignalKind.EVENT,
        sample_count=len(events),
        window=(times[0], times[-1]),
        times=bucket_t,
        values=bucket_v,
        stats={
            "frames": float(len(events)),
            "mean_rate": float(len(events) / span_s),
            "buckets": float(len(bucket_t)),
            "bucket_s": bucket_s,
        },
        note=f"{len(events)} 帧 → {len(bucket_t)} 个 {bucket_s * 1000:.1f} ms 桶",
        time_base=str(m.meta.get("time_base", "absolute")),
    )


def event_timeline(loaded: LoadedMeasurement, key: str) -> list[Event]:
    """取一个 (通道, ID) 的全部帧（按时间序）。"""
    channel, frame_id = _parse_event_key(key)
    return [
        ev for ev in loaded.document.events if ev.channel == channel and ev.frame_id == frame_id
    ]


@dataclass
class AlignedSignal:
    """两条信号对齐到公共栅格的结果。"""

    signal: str
    unit_a: str
    unit_b: str
    window: tuple[float, float]
    step: float
    grid: array
    a_values: array
    b_values: array
    diff_max: float
    diff_rms: float
    note: str = ""


def align_signal(
    loaded_a: LoadedMeasurement,
    loaded_b: LoadedMeasurement,
    signal: str,
    *,
    step: float = 0.01,
    window: tuple[float, float] | None = None,
) -> AlignedSignal:
    """把两次测量里的同一条信号对齐到公共栅格，给出差异统计。

    步骤：求两条序列时间范围的交集 → 在交集上按 `step` 建栅格 →
    两条序列各自线性插值上栅格 → 逐点作差。
    无交集时抛 ValueError —— 时间上不重叠的两次测量，
    「对齐后比较」的结论必然是假的，不如不给。
    """
    series_a = _find_signal(loaded_a.document, signal)
    series_b = _find_signal(loaded_b.document, signal)
    if series_a is None or series_b is None:
        missing = signal if series_a is None and series_b is None else (
            f"{signal}（仅在 {'A' if series_b is None else 'B'} 里有）"
        )
        raise KeyError(f"找不到信号 {missing}")

    times_a = _series_times(loaded_a, series_a)
    times_b = _series_times(loaded_b, series_b)
    values_a = load_series(loaded_a.source, series_a)
    values_b = load_series(loaded_b.source, series_b)
    if values_a is None or values_b is None:
        raise ValueError("有一条信号取不出值，无法对齐")

    span_a = (times_a[0], times_a[-1]) if len(times_a) else None
    span_b = (times_b[0], times_b[-1]) if len(times_b) else None
    common = common_window([span_a, span_b] if window is None else [window, span_a, span_b])
    if common is None:
        raise ValueError("两条信号的时间范围没有交集，无法对齐")

    grid = make_grid(common[0], common[1], step)
    a_on_grid = resample(times_a, values_a, grid)
    b_on_grid = resample(times_b, values_b, grid)
    diffs = array("d", (abs(x - y) for x, y in zip(a_on_grid, b_on_grid)))
    return AlignedSignal(
        signal=signal,
        unit_a=series_a.unit,
        unit_b=series_b.unit,
        window=common,
        step=step,
        grid=grid,
        a_values=a_on_grid,
        b_values=b_on_grid,
        diff_max=float(max(diffs)) if len(diffs) else 0.0,
        diff_rms=float(math.sqrt(math.fsum(d * d for d in diffs) / len(diffs)))
        if len(diffs)
        else 0.0,
        note=f"交集 {common[0]:.3f}–{common[1]:.3f} s，栅格 {len(grid)} 点",
    )


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------


def _series_times(loaded: LoadedMeasurement, series: SignalSeries) -> array:
    grp = _group_of(loaded.document, series)
    if grp is None:
        raise KeyError(f"信号 {series.name or series.path} 不属于任何通道组")
    times = ensure_time_axis(loaded.source, grp)
    if times is None:
        raise ValueError(f"信号 {series.name or series.path} 所在组没有时间主通道")
    return times


def _group_of(m: Measurement, series: SignalSeries) -> ChannelGroup | None:
    for grp in m.groups:
        if grp.master is series or series in grp.signals:
            return grp
    return None


def _find_signal(
    m: Measurement, name: str, group_index: int | None = None
) -> SignalSeries | None:
    groups = m.groups if group_index is None else [m.groups[group_index]]
    for grp in groups:
        for sig in grp.signals:
            if sig.path == name or sig.name == name:
                return sig
        if grp.master is not None and (
            grp.master.path == name or grp.master.name == name
        ):
            return grp.master
    return None


def _parse_event_key(key: str) -> tuple[int, int]:
    """`ch1/0x850` → (1, 0x850)。也接受 `ch1/2136`（十进制）。"""
    if "/" not in key:
        raise ValueError(
            f"事件键格式应为 ch<通道>/<ID>（如 ch1/0x850），得到 {key!r}"
        )
    ch_part, id_part = key.split("/", 1)
    if not ch_part.lower().startswith("ch"):
        raise ValueError(f"事件键须以 ch 开头（如 ch1/0x850），得到 {key!r}")
    channel = int(ch_part[2:])
    text = id_part.strip()
    frame_id = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    return channel, frame_id


def format_event_key(channel: int, frame_id: int) -> str:
    return f"ch{channel}/0x{frame_id:X}"
