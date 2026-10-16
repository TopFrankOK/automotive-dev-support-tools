"""能力声明。

统一骨架用「能力」而不是「类型」来描述一切可插拔的东西。
这样做的直接好处：新增一种数据来源或一种处理能力时，
不需要修改任何枚举式的 if/elif 分支，只需声明新能力。

两类能力刻意分开，因为它们回答的是不同的问题：
  - PluginCapability  插件「能提供什么」——由插件在 manifest 里声明
  - SourceCapability  数据源「能做什么」——由数据源在运行时探测
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["PluginCapability", "SourceCapability"]


class PluginCapability(StrEnum):
    """插件提供的能力，对应四类冻结扩展点及其扩展。"""

    PARSER = "parser"
    """文件 → 领域模型。新增一种文件格式 = 新增一个 parser 插件。"""

    ANALYZER = "analyzer"
    """领域模型 → 结论。比对、校验、信号计算都归这里。"""

    EXPORTER = "exporter"
    """领域模型 → 交付文件（Excel / Word / PDF / 报告）。"""

    VIEW = "view"
    """领域模型 → 界面面板。前端按 manifest 自动挂载。"""

    GATEWAY = "gateway"
    """与硬件或远端通道交互（CAN / 以太网 / DoIP / UDS）。"""

    RECIPE = "recipe"
    """刷写配方：描述某个 ECU 的刷写时序状态机。"""

    BRIDGE = "bridge"
    """外部工具桥（MATLAB 等）。本软件只做启动、传参与收结果。"""

    WRITER = "writer"
    """内存镜像 → 文件字节（写回）。

    刻意与 EXPORTER 分开：Exporter 产出**给人看的报告**，
    Writer 产出**要刷进 ECU 的文件**。失败后果完全不同 ——
    报告错了重导一遍就行，文件错了是数据事故。
    """


class SourceCapability(StrEnum):
    """数据源自身的能力，决定它适用于哪些操作模式。"""

    READ = "read"
    """可读。"""

    WRITE = "write"
    """可写回。CDM 的「修改后保存」要求数据源必须声明此项，
    且其 round-trip 必须逐字节保真。"""

    STREAM = "stream"
    """可流式读取，适用于 GB 级测量文件，不必全量载入内存。"""

    SEEK = "seek"
    """支持随机访问（按时间戳或地址定位）。"""

    LIVE = "live"
    """在线且**有状态**。刷写属于此类：不可重放，
    中断后必须走断点续刷而不是重跑。"""
