"""降采样（抽稀）—— 把百万级样本压成屏幕画得下的量级。

## 为什么不能「每隔 N 个取一个」

等距抽样会**系统性丢尖峰**：一个 2 ms 的转速尖峰落在两个抽样点之间，
趋势图上就再也看不到它。而工程上恰恰是这些尖峰决定要不要停车检查。

所以这里提供两条路：

    minmax_bands  每桶保留 (min, max) —— 画成竖线带，尖峰以「高度」幸存
    bucketize     每桶聚合成一个值（mean/max/min）—— 需要单值序列时用

趋势视图的标准做法是 `minmax_bands`：像素列数就是桶数，
一列里从 min 到 max 拉一条竖线。信息量与原始数据同阶，
体积却从 N 降到 2×桶数。
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass

__all__ = ["MinMaxBand", "minmax_bands", "bucketize"]


@dataclass(frozen=True)
class MinMaxBand:
    """一个抽稀桶。画趋势时是一根从 v_min 到 v_max 的竖线。"""

    t_start: float
    t_end: float
    v_min: float
    v_max: float
    count: int = 0
    """桶内样本数。全 0 或全同值的桶在界面上可以淡化显示。"""


def minmax_bands(
    times: array | list[float],
    values: array | list[float],
    max_points: int,
) -> list[MinMaxBand]:
    """按样本序号分桶，产出不超过 `max_points` 个 min/max 频带。

    按序号而不是按时间分桶：时间分桶在信号有缺口时会产出空桶，
    而序号分桶保证每个桶都有内容、总数严格受控。
    时间信息仍然保留在 `t_start` / `t_end` 里。
    """
    n = min(len(times), len(values))
    if n == 0 or max_points <= 0:
        return []
    if n <= max_points:
        # 不需要抽稀 —— 逐点成带，一个样本一根「线」
        return [
            MinMaxBand(times[i], times[i], values[i], values[i], 1)
            for i in range(n)
        ]

    bands: list[MinMaxBand] = []
    # 桶大小向上取整，保证 total_bands <= max_points
    step = math.ceil(n / max_points)
    for start in range(0, n, step):
        end = min(start + step, n)
        chunk = values[start:end]
        lo = min(chunk)
        hi = max(chunk)
        bands.append(
            MinMaxBand(times[start], times[end - 1], lo, hi, end - start)
        )
    return bands


def bucketize(
    times: array | list[float],
    values: array | list[float],
    bucket_s: float,
    stat: str = "mean",
) -> tuple[array, array]:
    """按时间桶聚合成单值序列，返回 (桶起点时间, 聚合值)。

    `stat` 取 `mean` / `max` / `min` / `first` / `last` / `sum`。
    `sum` 是给事件流数帧用的（每帧记 1，桶内求和即帧数）。
    桶宽 `bucket_s`（秒）固定，从序列起点开始切 ——
    起点对齐保证两次调用（不同通道、不同时间窗）结果可直接相减比较。
    """
    if bucket_s <= 0:
        raise ValueError(f"桶宽必须为正，得到 {bucket_s}")
    n = min(len(times), len(values))
    if n == 0:
        return array("d"), array("d")

    t0 = times[0]
    # 桶索引直接由时间决定：同一时刻的任何序列，桶边界完全一致
    buckets: dict[int, list[float]] = {}
    for i in range(n):
        idx = int((times[i] - t0) // bucket_s)
        buckets.setdefault(idx, []).append(values[i])

    out_t = array("d")
    out_v = array("d")
    for idx in sorted(buckets):
        chunk = buckets[idx]
        if stat == "max":
            value = max(chunk)
        elif stat == "min":
            value = min(chunk)
        elif stat == "first":
            value = chunk[0]
        elif stat == "last":
            value = chunk[-1]
        elif stat == "sum":
            value = math.fsum(chunk)
        else:
            value = math.fsum(chunk) / len(chunk)
        out_t.append(t0 + idx * bucket_s)
        out_v.append(value)
    return out_t, out_v
