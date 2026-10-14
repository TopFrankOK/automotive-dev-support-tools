"""端口层：内核与插件之间的合同。

这里只有「接口」和「注册表」，没有任何实现。
任何具体的解析、导出、通信实现都必须落在 `plugins/` 或 `platform/`。
"""

from automotive_dev_support.core.ports.plugin import (
    API_VERSION,
    AnalysisInput,
    AnalysisOutcome,
    Analyzer,
    Exporter,
    Gateway,
    ParseOutcome,
    Parser,
    PluginManifest,
    View,
)
from automotive_dev_support.core.ports.registry import LoadedPlugin, PluginRegistry

__all__ = [
    "API_VERSION",
    "PluginManifest",
    "ParseOutcome",
    "AnalysisOutcome",
    "AnalysisInput",
    "Parser",
    "Analyzer",
    "Exporter",
    "View",
    "Gateway",
    "PluginRegistry",
    "LoadedPlugin",
]
