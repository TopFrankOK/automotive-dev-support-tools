"""测量数据领域模型 —— 「什么时候，发生了什么」。

这是数据分析功能域的地基。它与 `models.calibration` 的关系是
**对照**而不是包含：标定模型回答「这个量应该是什么」，
测量数据回答「它实际是什么」。两者的汇合在分析用例里。

## 两种形态刻意分开

    CONTINUOUS  连续采样：时间轴 + 等距/近等距的数值序列（MDF4 传感器信号）
    EVENT       离散事件：一帧一事件，只有时刻没有间隔（CAN 报文）

把两者塞进同一个「信号」抽象是经典的建模错误 ——
连续信号可以做插值、重采样、降采样，事件不能。
强行统一的结果是每个消费方都要先 `if kind == EVENT` 再干活。
所以这里分开，共享的只有 `SignalSeries` 这个名字。

## 懒加载是一等公民

MF4 动辄几百 MB 到几 GB。解析时**只建索引不取值**：
`DataLocation` 记录「样本在文件哪个偏移、记录多长、怎么解码」，
真正的读取由用例层按需发起（见 `decode.py`）。
`values is None` 就代表「还没读」—— 模型自身永远不做 IO。
"""

from __future__ import annotations

from array import array
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from automotive_dev_support.core.models.common import now_utc

__all__ = [
    "SignalKind",
    "ValueDtype",
    "DataTypeSpec",
    "DataLocation",
    "Event",
    "EventStat",
    "SignalSeries",
    "ChannelGroup",
    "Measurement",
]


class SignalKind(StrEnum):
    CONTINUOUS = "continuous"
    """连续采样信号：有主通道（通常是时间），可重采样。"""

    EVENT = "event"
    """离散事件：CAN 帧等。只有时刻，没有间隔。"""


class ValueDtype(StrEnum):
    UINT = "uint"
    INT = "int"
    FLOAT = "float"


class DataTypeSpec(BaseModel):
    """一个通道值的存储编码。

    与 MDF4 的设计对齐：**类型类 + 位宽 + 字节序**三者才能确定布局。
    位宽不一定是 8 的倍数（位压缩信号），那种通道本模型不解码 ——
    见 `decode.py` 的约定。
    """

    dtype: ValueDtype = ValueDtype.FLOAT
    bit_count: int = 64
    byte_order: str = "little"
    """"little" 或 "big"。"""

    @property
    def byte_size(self) -> int:
        return (self.bit_count + 7) // 8

    @property
    def byte_aligned(self) -> bool:
        """是否字节对齐。位压缩信号返回 False。"""
        return self.bit_count % 8 == 0


class DataLocation(BaseModel):
    """样本在文件中的位置 —— 懒加载的全部依据。

    核心只回答「在哪、怎么解」，**不碰 IO**：
    偏移是绝对文件偏移，步长是记录长度，样本在记录内还有自己的偏移。
    三者凑齐就能顺序抽出任意一个通道，不必整体载入。
    """

    uri: str = ""
    """来源文件的 URI。空字符串表示调用方自行补全。"""

    start: int = 0
    """首条样本的绝对字节偏移。"""

    stride: int = 0
    """记录步长（字节）。样本 i 位于 `start + i * stride`。"""

    offset: int = 0
    """样本在记录内的字节偏移。"""

    count: int = 0
    """样本条数。"""

    spec: DataTypeSpec = Field(default_factory=DataTypeSpec)

    spans: list[tuple[int, int]] = Field(default_factory=list)
    """多段数据区：(文件偏移, 字节数) 列表。空 = 单段
    （起点 `start`，长度 `count * stride`）。

    MDF4 的大通道组数据会跨多个 ##DT 块存放，记录在文件里
    不再连续。装载时按段拼接成连续记录流再解码 ——
    对上层仍然是「一个数据区」，只是物理上分了几段。
    """

    @property
    def span(self) -> int:
        """整个数据区跨过的字节数。"""
        return self.count * self.stride

    @property
    def total_bytes(self) -> int:
        """物理字节总数（含段间不连续造成的重复计算，仅用于估算）。"""
        if self.spans:
            return sum(length for _off, length in self.spans)
        return self.span


class Event(BaseModel):
    """一个离散事件。CAN 帧是主角，错误帧与标记也走这里。"""

    t: float
    """时刻（秒）。相对还是绝对由所属 Measurement 的 meta 说明。"""

    channel: int = 0
    """总线通道号。与源文件一致（Vector 格式为 1 基）。"""

    frame_id: int = 0
    """报文 ID（已去掉扩展标志）。"""

    data: bytes = b""
    dlc: int = 0
    """DLC。远程帧与 CAN FD 帧的数据长度与 DLC 不是一回事，分别记。"""

    direction: str = "rx"
    """"rx" / "tx"。"""

    kind: str = "can"
    """"can" / "remote" / "error" / "marker"。"""

    extended: bool = False
    is_fd: bool = False


class EventStat(BaseModel):
    """一个 (通道, 报文 ID) 的统计摘要 —— 事件流的「索引」。

    总线日志动辄几十万到上亿帧，全量驻留内存不现实。
    所以统计（计数、首末时刻、字节数）**永远全量**，
    帧体只保留有界的一段（见 `Measurement.events` 与解析器的
    `max_events`）。总线负载、报文周期这些分析只看统计就够。
    """

    count: int = 0
    first_t: float = 0.0
    last_t: float = 0.0
    bytes: int = 0


class SignalSeries(BaseModel):
    """一条序列。连续型带 `location`（懒加载）或 `values`（已载入）；
    事件型带 `events`。三者互斥使用，不会同时出现。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: str
    """显示路径。连续型通常是信号名；事件型是 `ch<通道>/<ID十六进制>`。"""

    name: str = ""
    unit: str = ""
    kind: SignalKind = SignalKind.CONTINUOUS
    group: int = 0
    """所属通道组（MDF4 的 CG 序号）。"""

    spec: DataTypeSpec | None = None
    location: DataLocation | None = None
    values: array | None = None
    """已载入的样本。连续型主通道存**纳秒整数**（精度优先），见 `ChannelGroup.seconds`。"""

    events: list[Event] | None = None

    @property
    def loaded(self) -> bool:
        return self.values is not None

    @property
    def sample_count(self) -> int:
        if self.values is not None:
            return len(self.values)
        if self.location is not None:
            return self.location.count
        if self.events is not None:
            return len(self.events)
        return 0


class ChannelGroup(BaseModel):
    """一组联合采样的通道 + 它们的时间主通道。

    MDF4 里一个 CG 一条主通道（多数是时间）。不同组可以有不同的
    采样率与时间栅格 —— 这就是「公共栅格对齐」要解决的问题。
    """

    index: int
    name: str = ""
    master: SignalSeries | None = None
    signals: list[SignalSeries] = Field(default_factory=list)

    def seconds(self) -> array | None:
        """主通道时间轴，换算成**相对首样本的秒数**（t=0 在起点）。

        主通道原始值是纳秒整数（int64 精确存），除以 1e9 得秒。
        趋势视图、重采样、对齐全部在这个相对时间轴上做 ——
        绝对时刻只在跨文件对照时才需要，那时用 `epoch_seconds()`。
        """
        if self.master is None or self.master.values is None:
            return None
        raw = self.master.values
        if not len(raw):
            return array("d")
        origin = raw[0]
        return array("d", ((v - origin) / 1e9 for v in raw))

    def epoch_seconds(self) -> array | None:
        """主通道时间轴，Unix 纪元秒（浮点，跨文件对照用）。"""
        if self.master is None or self.master.values is None:
            return None
        return array("d", (v / 1e9 for v in self.master.values))

    def signal(self, path: str) -> SignalSeries | None:
        for sig in self.signals:
            if sig.path == path or sig.name == path:
                return sig
        return None


class Measurement(BaseModel):
    """一次测量：若干连续组 + 一个事件流。"""

    label: str = ""
    start_time: datetime | None = None
    """记录起始时刻（文件头声明）。事件流的绝对时刻以此为锚。"""

    groups: list[ChannelGroup] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    event_stats: dict[tuple[int, int], EventStat] = Field(default_factory=dict)
    """按 (通道, 报文 ID) 的全量统计。`frame_counts()` 优先用它。"""

    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)

    # -- 查询 ----------------------------------------------------------------

    def signals(self) -> list[SignalSeries]:
        out: list[SignalSeries] = []
        for grp in self.groups:
            if grp.master is not None:
                out.append(grp.master)
            out.extend(grp.signals)
        return out

    def signal(self, path: str) -> SignalSeries | None:
        for sig in self.signals():
            if sig.path == path or sig.name == path:
                return sig
        return None

    def signal_count(self) -> int:
        return len(self.signals())

    def loaded_count(self) -> int:
        return sum(1 for sig in self.signals() if sig.loaded)

    def time_range(self) -> tuple[float, float] | None:
        """所有已载入序列覆盖的时间范围（相对秒）。无内容返回 None。"""
        lo: float | None = None
        hi: float | None = None
        for grp in self.groups:
            times = grp.seconds()
            if times and len(times):
                lo = times[0] if lo is None else min(lo, times[0])
                hi = times[-1] if hi is None else max(hi, times[-1])
        if self.events:
            first, last = self.events[0].t, self.events[-1].t
            lo = first if lo is None else min(lo, first)
            hi = last if hi is None else max(hi, last)
        if lo is None or hi is None:
            return None
        return (lo, hi)

    def duration(self) -> float | None:
        span = self.time_range()
        return None if span is None else span[1] - span[0]

    # -- 事件流 ---------------------------------------------------------------

    def events_by_id(self) -> dict[tuple[int, int], list[Event]]:
        """按 (通道, 报文 ID) 分组。同一 ID 的帧序列就是这条报文的时间线。"""
        out: dict[tuple[int, int], list[Event]] = {}
        for ev in self.events:
            out.setdefault((ev.channel, ev.frame_id), []).append(ev)
        return out

    def frame_counts(self) -> dict[tuple[int, int], int]:
        """每帧计数。优先用全量统计；没有统计（小文件直接载入）才数 events。"""
        if self.event_stats:
            return {key: stat.count for key, stat in self.event_stats.items()}
        counts: dict[tuple[int, int], int] = {}
        for ev in self.events:
            key = (ev.channel, ev.frame_id)
            counts[key] = counts.get(key, 0) + 1
        return counts
