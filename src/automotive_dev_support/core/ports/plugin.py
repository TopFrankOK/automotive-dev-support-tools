"""冻结的扩展点契约。

这里是全系统最重要的一份「不许随便改」的代码。
原因：每一个扩展点都是插件与内核之间的合同。
你改一次，所有实现了它的插件都要跟着改 —— 那正是我们要避免的事。

所以本模块的修改需要单独 review，并遵守两条规则：

  1. **扩展点只增不改。** 需要新能力时，加一个新的 Protocol，
     而不是往已有 Protocol 里加方法。
  2. **数据形状用 dict / Artifact / 领域模型传递，不用位置参数。**
     这样加字段不会破坏已有实现。

`API_VERSION` 是这套契约的版本号。插件在 manifest 里声明自己
针对哪个版本开发，注册表在加载时校验，避免旧插件悄悄跑错。
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from automotive_dev_support.core.skeleton.artifact import Artifact, ArtifactKind
from automotive_dev_support.core.skeleton.capability import PluginCapability
from automotive_dev_support.core.skeleton.datasource import DataSource
from automotive_dev_support.core.skeleton.job import JobContext

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
]

#: 扩展点契约版本。破坏性变更时才 +1。
API_VERSION = "1"


class PluginManifest(BaseModel):
    """插件清单（`manifest.toml`）。

    清单是插件的**全部对外声明**。注册表只读清单，不读代码，
    因此一个损坏的插件最多是加载失败，不会拖垮整个应用。
    """

    id: str
    name: str = ""
    version: str = "0.1.0"
    api_version: str = API_VERSION

    capabilities: list[PluginCapability] = Field(default_factory=list)
    """本插件提供哪些能力。注册表按能力建立索引。"""

    entry: str
    """入口，格式 `"package.module:Attribute"`。属性可以是类或工厂函数。"""

    description: str = ""
    author: str = ""

    requires: list[str] = Field(default_factory=list)
    """运行本插件所需的授权功能项。对应许可码的 features。"""

    handles: list[ArtifactKind] = Field(default_factory=list)
    """处理哪些格式。仅 parser 有意义，但声明在这里便于界面提示。"""

    ui: str | None = None
    """前端面板入口（相对插件目录的路径）。
     前端用 import.meta.glob 扫描同一字段自动挂载，后端不需要管它。"""

    enabled: bool = True

    @field_validator("entry")
    @classmethod
    def _entry_shape(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("entry 必须形如 'pkg.module:Attr'")
        return v

    @field_validator("capabilities")
    @classmethod
    def _has_capability(cls, v: list[PluginCapability]) -> list[PluginCapability]:
        if not v:
            raise ValueError("插件必须至少声明一种能力")
        return v

    @classmethod
    def from_toml(cls, path: Path) -> PluginManifest:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        return cls.model_validate(raw.get("plugin", raw))


# ---------------------------------------------------------------------------
# 四类冻结扩展点
# ---------------------------------------------------------------------------


class ParseOutcome(BaseModel):
    """解析结果。

    `document` 是领域模型（`core.models.*` 里的某个类型）。
    契约层不 import 具体领域模型，是为了让「新增领域」不必改动本模块 ——
    这正是扩展点只增不改的落地方式。
    """

    model_config = {"arbitrary_types_allowed": True}

    artifact: Artifact
    document: Any = None
    """领域模型实例。消费方按 `artifact.role` 决定如何解释它。"""

    warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class Parser(Protocol):
    """文件 → 领域模型。

    实现要求：
      - **必须无损。** 凡是被声明 WRITE 的格式，解析时必须保留
        所有未被识别的字节，否则写回会破坏工具链兼容性。
      - 大文件必须走 `DataSource.stream()`，不要 `read_all()`。
    """

    kinds: frozenset[ArtifactKind]

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome: ...


class AnalysisOutcome(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    payload: Any = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class AnalysisInput:
    """一件待分析的输入：工件元数据 + 已解析出的领域模型。

    刻意把两者绑在一起，而不是让 Analyzer 自己去某个注册表里找 ——
    后者会让插件隐式依赖「谁先跑了什么」的执行顺序，
    那种耦合在插件数量上来之后极难排查。
    """

    artifact: Artifact
    document: Any = None


@runtime_checkable
class Analyzer(Protocol):
    """领域模型 → 结论。比对、校验、信号计算都实现这个协议。

    约定：Analyzer **不修改输入**。它产出新的结论对象。
    需要「改动后保存」的场景走独立的 Editable 路径，
    不要让它出现在 Analyzer 里 —— 否则「比对」和「编辑」两种语义会混淆，
    而这两种语义的失败后果完全不同：比对错了可以重跑，编辑错了是数据事故。
    """

    def analyze(
        self,
        inputs: Sequence[AnalysisInput],
        params: dict[str, Any],
        ctx: JobContext,
    ) -> AnalysisOutcome: ...


@runtime_checkable
class Exporter(Protocol):
    """领域模型 → 交付文件。Excel / Word / PDF / HTML 都归这里。"""

    media_type: str
    """例如 application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"""

    extension: str

    def export(
        self,
        payload: Any,
        target: Path,
        params: dict[str, Any],
        ctx: JobContext,
    ) -> Path: ...


@runtime_checkable
class View(Protocol):
    """界面面板。

    后端**不实现**这个协议 —— 它只是前端资源的声明。
    后端认识它的唯一原因：在 API 里把可用面板列出来。
    """

    panel_id: str
    title: str


@runtime_checkable
class Gateway(Protocol):
    """与硬件或远端通道交互。

    实现在独立进程中运行（见架构的「进程边界」）。
    因此本协议的方法签名必须**可序列化**：
    传 dict / bytes，不要传对象引用或回调。
    """

    channel: str
    """例如 "can0" / "eth:192.168.1.10" / "doip:0x0E80" """

    def connect(self, params: dict[str, Any]) -> None: ...

    def disconnect(self) -> None: ...

    def request(self, payload: bytes, timeout_s: float = 1.0) -> bytes: ...

    @property
    def connected(self) -> bool: ...
