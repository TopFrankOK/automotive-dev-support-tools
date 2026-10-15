"""测量数据的装载与查询用例。

解析器只建索引（`DataLocation`），取值在这里发生。
「读」与「解」的分工沿袭 core 的约定：本模块负责把字节读出来
（按 spans 拼接），解码交给 `core.models.measurement.decode`。

## 为什么加载器在 services 而不在 core

core 是零 IO 的地基。`DataLocation` 描述「数据在哪」是纯数据；
「把数据读出来」是副作用，属于用例编排层。解析器插件同样不做这事 ——
它们连 `platform.store` 都不 import（契约 2），只接收 DataSource。
"""

from __future__ import annotations

from array import array

from automotive_dev_support.core.models.measurement import (
    ChannelGroup,
    DataLocation,
    Measurement,
    SignalSeries,
)
from automotive_dev_support.core.models.measurement.decode import decode_samples
from automotive_dev_support.core.skeleton import DataSource

__all__ = ["read_records", "load_series", "load_group", "ensure_time_axis"]


def read_records(source: DataSource, location: DataLocation) -> bytes:
    """把 location 描述的数据区完整读进内存。

    多段（MDF4 跨 DT 块）时按段拼接 —— 对上层仍是「一个数据区」。
    单段时一次 `read_at`，不整体载入文件。
    """
    if location.spans:
        return b"".join(
            source.read_at(offset, length) for offset, length in location.spans
        )
    if location.count <= 0 or location.stride <= 0:
        return b""
    return source.read_at(location.start, location.count * location.stride)


def load_series(source: DataSource, series: SignalSeries) -> array | None:
    """按需装载一条序列。已载入的直接返回（幂等）。

    位压缩 / 位宽不支持 / 数据区被截断时返回 None ——
    宁缺不假，调用方应把这种情况告诉用户。
    """
    if series.values is not None:
        return series.values
    location = series.location
    if location is None:
        return None
    raw = read_records(source, location)
    values = decode_samples(
        raw, location.spec, location.count, location.stride, location.offset
    )
    if values is not None:
        series.values = values
    return values


def load_group(source: DataSource, group: ChannelGroup) -> bool:
    """装载整组（含主通道）。任一通道解不动返回 False。"""
    ok = True
    if group.master is not None:
        ok = load_series(source, group.master) is not None and ok
    for sig in group.signals:
        if load_series(source, sig) is None:
            ok = False
    return ok


def ensure_time_axis(source: DataSource, group: ChannelGroup) -> array | None:
    """确保主通道已载入并返回秒时间轴（相对首样本）。

    没有主通道的组返回 None —— 那种组只能按记录序号对照。
    """
    if group.master is None:
        return None
    if load_series(source, group.master) is None:
        return None
    return group.seconds()


def measurement_time_range(measurement: Measurement) -> tuple[float, float] | None:
    """已载入序列覆盖的时间范围。全部未载入时返回 None。"""
    return measurement.time_range()
