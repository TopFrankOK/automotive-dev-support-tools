"""测量数据领域 —— 时间轴上的世界。

对外暴露三层，按依赖顺序：

    model      数据形状：Measurement / ChannelGroup / SignalSeries / Event
    decode     字节 → 样本（纯计算，配合 DataLocation 懒加载）
    decimate   样本 → 可画的量级（min/max 频带、时间桶聚合）
    align      两段录音 → 同一把时间尺（公共窗、重采样、平移）

`model` 不做 IO；IO 在用例层（`services.measurement`）与解析插件里。
"""

from automotive_dev_support.core.models.measurement.model import (
    ChannelGroup,
    DataLocation,
    DataTypeSpec,
    Event,
    EventStat,
    Measurement,
    SignalKind,
    SignalSeries,
    ValueDtype,
)
from automotive_dev_support.core.models.measurement.decode import decode_samples
from automotive_dev_support.core.models.measurement.decimate import (
    MinMaxBand,
    bucketize,
    minmax_bands,
)
from automotive_dev_support.core.models.measurement.align import (
    common_window,
    make_grid,
    resample,
    shift,
)

__all__ = [
    # model
    "SignalKind",
    "ValueDtype",
    "DataTypeSpec",
    "DataLocation",
    "Event",
    "EventStat",
    "SignalSeries",
    "ChannelGroup",
    "Measurement",
    # decode
    "decode_samples",
    # decimate
    "MinMaxBand",
    "minmax_bands",
    "bucketize",
    # align
    "common_window",
    "make_grid",
    "resample",
    "shift",
]
