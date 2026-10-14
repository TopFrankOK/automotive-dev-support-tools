"""标定比对用例 —— 应用层。

**为什么 CLI 不直接调插件：** 命令行只是这个用例的一个入口，
将来 FastAPI 服务、桌面界面都会走同一条路。用例逻辑放这里，
入口层（apps）才薄得起来。

数据流（全部经注册表拿插件，不 import 任何具体插件）：

    A2L ──parser-a2l──▶ CalibrationModel ─┐
                                          ├─▶ bind_values / bind_image ─▶ 挂值模型
    CDFX ─parser-cdfx─▶ ValueSet ─────────┘         （二选一，或都不给）
    HEX/S19 ─parser─▶ AddressSpace ───────┘

    两个挂值模型 ──diff-calibration──▶ ChangeSet ──export-excel-diff──▶ xlsx
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automotive_dev_support.core.models.calibration import (
    CalibrationModel,
    ChangeSet,
    ValueBinding,
    bind_image,
    bind_values,
)
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, JobContext
from automotive_dev_support.platform.store import FileSource, kind_from_suffix

__all__ = [
    "CalibrationSources",
    "LoadedCalibration",
    "CompareOutcome",
    "load_calibration",
    "run_compare",
]


@dataclass(frozen=True)
class CalibrationSources:
    """一份标定数据的来源文件。A2L 必填，值与镜像二选一（或都不给）。"""

    a2l: Path
    values: Path | None = None
    """值集文件（CDFX 等）—— 与 image 二选一。"""

    image: Path | None = None
    """内存镜像（HEX / S19）—— 与 values 二选一。"""

    label: str = ""
    byte_order: str = "big"


@dataclass
class LoadedCalibration:
    model: CalibrationModel
    binding: ValueBinding | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class CompareOutcome:
    baseline: LoadedCalibration
    candidate: LoadedCalibration
    changeset: ChangeSet
    report_path: Path | None = None


def load_calibration(
    registry: PluginRegistry, sources: CalibrationSources
) -> LoadedCalibration:
    """解析一份标定数据：A2L 必选，值集/镜像可选。"""
    ctx = JobContext(job_id="load")
    warnings: list[str] = []

    model, parse_warnings = _parse(registry, ArtifactKind.A2L, sources.a2l, ctx)
    warnings.extend(f"{sources.a2l.name}: {w}" for w in parse_warnings)
    if not isinstance(model, CalibrationModel):
        raise TypeError(f"{sources.a2l} 没有解析出标定模型（得到 {type(model).__name__}）")

    binding: ValueBinding | None = None
    if sources.values is not None:
        valueset, value_warnings = _parse(
            registry, kind_from_suffix(sources.values), sources.values, ctx
        )
        warnings.extend(f"{sources.values.name}: {w}" for w in value_warnings)
        model, binding = bind_values(model, valueset)
        warnings.extend(f"值集绑定：{w}" for w in binding.warnings)
    elif sources.image is not None:
        image, image_warnings = _parse(
            registry, kind_from_suffix(sources.image), sources.image, ctx
        )
        warnings.extend(f"{sources.image.name}: {w}" for w in image_warnings)
        model, binding = bind_image(model, image, byte_order=sources.byte_order)
        warnings.extend(f"镜像取值：{w}" for w in binding.warnings)
    else:
        warnings.append("未提供值集或内存镜像 —— 只比对描述信息，不比对数值")

    return LoadedCalibration(model=model, binding=binding, warnings=warnings)


def run_compare(
    registry: PluginRegistry,
    baseline: CalibrationSources,
    candidate: CalibrationSources,
    out: Path | None = None,
    params: dict[str, Any] | None = None,
) -> CompareOutcome:
    """端到端：加载两份 → 比对 → （可选）导出报告。"""
    left = load_calibration(registry, baseline)
    right = load_calibration(registry, candidate)

    differ = _single(registry, "analyzer", "diff-calibration")
    outcome = differ.analyze(
        [
            _input(left.model, baseline),
            _input(right.model, candidate),
        ],
        params or {},
        JobContext(job_id="compare"),
    )
    changeset: ChangeSet = outcome.payload
    for warning in outcome.warnings:
        left.warnings.append(f"比对：{warning}")

    report_path = None
    if out is not None:
        exporter = _single(registry, "exporter", "export-excel-diff")
        report_path = exporter.export(
            changeset, out, params or {}, JobContext(job_id="export")
        )

    return CompareOutcome(
        baseline=left, candidate=right, changeset=changeset, report_path=report_path
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _parse(
    registry: PluginRegistry, kind: ArtifactKind, path: Path, ctx: JobContext
) -> tuple[Any, list[str]]:
    """经注册表找解析器并解析，返回 (领域模型, 告警)。"""
    parsers = registry.parsers_for(kind)
    if not parsers:
        raise ValueError(f"没有能处理 {kind.value} 格式的解析器插件（文件：{path}）")
    outcome = parsers[0].instance.parse(FileSource(path), ctx)
    return outcome.document, list(outcome.warnings)


def _single(registry: PluginRegistry, capability: str, plugin_id: str):
    from automotive_dev_support.core.skeleton.capability import PluginCapability

    plugins = registry.by_capability(PluginCapability(capability))
    for plugin in plugins:
        if plugin.manifest.id == plugin_id:
            return plugin.instance
    raise ValueError(f"插件 {plugin_id} 不可用（能力 {capability}）")


def _input(model: CalibrationModel, sources: CalibrationSources):
    from automotive_dev_support.core.ports.plugin import AnalysisInput
    from automotive_dev_support.core.skeleton.artifact import Artifact, ArtifactRef

    return AnalysisInput(
        artifact=Artifact(
            kind=ArtifactKind.A2L,
            ref=ArtifactRef(uri=str(sources.a2l)),
            label=sources.label or sources.a2l.name,
        ),
        document=model,
    )
