"""时间对齐 —— 让两段录音、两条信号站在同一把尺子上。

三个场景，对应三个函数：

    common_window   两次测量到底重合了哪一段（先求交，再干别的）
    resample        把一条序列插到公共栅格上（不同采样率的信号放一起比）
    shift           整体平移（触发时刻对不齐时手工拨）

## 为什么插值用「线性」而不用「保持」

保持（step）会把台阶伪影带进比对：两条同源信号只因采样相位差半格，
保持插值就能给出 5% 的假差异。线性插值在过采样区间的误差是二阶小量，
工程比对够用，且不会无中生有。
"""

from __future__ import annotations

from array import array

__all__ = ["common_window", "resample", "shift"]


def common_window(
    ranges: list[tuple[float, float] | None],
) -> tuple[float, float] | None:
    """求多个 `(起, 止)` 区间的交集。无交集（或全空）返回 None。

    交集为空是**有价值的信息**：它说明这两段录音在时间上根本不重叠，
    任何「对齐后比较」的结论都是假的。所以返回 None 而不是退化成并集。
    """
    lo: float | None = None
    hi: float | None = None
    for span in ranges:
        if span is None:
            continue
        start, end = span
        if end <= start:
            continue
        lo = start if lo is None else max(lo, start)
        hi = end if hi is None else min(hi, end)
    if lo is None or hi is None or hi <= lo:
        return None
    return (lo, hi)


def resample(
    times: array | list[float],
    values: array | list[float],
    grid: array | list[float],
) -> array:
    """把 `(times, values)` 线性插值到 `grid` 上。

    `grid` 必须已排序。栅格落在序列范围之外的点取**端点值**
    （截断，不外推）—— 外推出来的趋势是编造的，截断至少是诚实的。
    """
    n = min(len(times), len(values))
    if n == 0 or len(grid) == 0:
        return array("d")
    if n == 1:
        return array("d", [values[0]] * len(grid))

    out = array("d")
    j = 0
    for t in grid:
        if t <= times[0]:
            out.append(values[0])
            continue
        if t >= times[n - 1]:
            out.append(values[n - 1])
            continue
        while j < n - 2 and times[j + 1] < t:
            j += 1
        t0, t1 = times[j], times[j + 1]
        v0, v1 = values[j], values[j + 1]
        if t1 == t0:
            out.append(v0)
        else:
            out.append(v0 + (v1 - v0) * (t - t0) / (t1 - t0))
    return out


def make_grid(start: float, end: float, step: float) -> array:
    """构造 `[start, end]` 上的等距栅格（含终点，容差内）。"""
    if step <= 0:
        raise ValueError(f"栅格步长必须为正，得到 {step}")
    if end <= start:
        return array("d")
    count = int((end - start) / step)
    grid = array("d", (start + i * step for i in range(count + 1)))
    if grid[-1] > end:
        grid.pop()
    return grid


def shift(
    times: array | list[float],
    values: array | list[float],
    delta: float,
) -> tuple[array, array]:
    """整条序列平移 `delta` 秒。返回新序列，不改输入。"""
    return (
        array("d", (t + delta for t in times[: len(values)])),
        array("d", values),
    )
