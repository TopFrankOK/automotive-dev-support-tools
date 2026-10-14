"""信号时序领域 —— 数据分析（待实现）。

第二期建设。现在留目录与契约位置，是为了让 `plugins` 侧
（`parser-mf4` / `parser-blf` / `signal-compute` / `view-trend`）
有一个明确的目标，而不是等到做的时候才发现模型没设计。

## 已确定的设计约束（先写下来，避免将来走偏）

1. **数据不进内存。** GB 级的 MF4/BLF 必须走 `DataSource.stream()`，
   在旁挂的 `.idx` 索引里记录「通道表 / 时间戳索引 / 块偏移」。
   模型只持有索引与统计量，通道数据按需分块读取。

2. **降采样在后端做。** 视图需要的是「形状」不是「全部点」。
   后端按 MinMax / FirstLast 金字塔降采样后交给前端 Canvas 渲染。
   把原始点丢给前端是撑不住 GB 级数据的。

3. **信号计算是 Analyzer 插件。** 类似 CANape 的 Formula。
   表达式求值引擎是关键可扩展点 —— 用户要加自定义算法时，
   应该只是加一个插件，不需要动主程序。

## 计划中的实体

    Signal           一个通道：名称、单位、采样率、数据类型、块索引
    TimeAxis         时间轴：起始时刻、采样间隔、点数
    SignalGroup      通道分组（对应测量文件里的一个 measurement）
    MeasurementModel 一次测量会话的完整索引视图
    SignalStatistics 统计量：min/max/mean/rms/std，用于快览与降采样边界

`plugins/parser-mf4` 实现 `Parser` 后，产出 `MeasurementModel` 作为
`ParseOutcome.document`。
"""

from __future__ import annotations

__all__: list[str] = []
