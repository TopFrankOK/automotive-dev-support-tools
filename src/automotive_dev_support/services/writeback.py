"""写回编排：值集 → 内存镜像 → 文件。

## 它串起来的每一步，顺序都不能换

    解析镜像 → 解析描述 → 绑定值 → 写值 → 重算校验和 → 序列化 → 原子落盘

**校验和必须在序列化之前算**：算的是镜像里被改过的字节，
先序列化再算就等于把值算进了旧文件，白算。

**落盘必须是最后一步**：任何一步失败，原文件都还是完好的。
这也是为什么序列化产出字节、由数据源去落盘 —— 插件不做 IO，
"写到一半崩了不留半截"由数据源统一保证。

## 失败即停，并说清停在哪一步

写回中途失败却继续往下走，是最危险的一类行为：
它会产出一个**看起来完整、其实缺了改动**的文件。
所以这里任何一步出错都抛 `WritebackError` 并说明停在哪一步。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from automotive_dev_support.core.models.calibration import CalibrationModel
from automotive_dev_support.core.models.calibration.values import (
    apply_values,
    bind_values,
)
from automotive_dev_support.core.models.memory import AddressSpace, ChecksumError, ChecksumSpec
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, JobContext
from automotive_dev_support.core.skeleton.capability import PluginCapability
from automotive_dev_support.platform.store import FileSource, kind_from_suffix

__all__ = [
    "WritebackError",
    "WritebackSources",
    "WritebackOutcome",
    "run_writeback",
]


class WritebackError(RuntimeError):
    """写回失败。`WritebackError` 自带「停在哪一步」的信息。"""


@dataclass(frozen=True)
class WritebackSources:
    """写回的输入。"""

    image: Path
    """被改写的内存镜像（HEX 等）。"""

    a2l: Path
    """描述文件：提供每个量的地址、类型与字节宽度。"""

    values: Path | None = None
    """值集文件（CDFX / DCM）—— 要写进去的那些值。"""

    byte_order: str = "big"


@dataclass
class WritebackOutcome:
    written: list[str] = field(default_factory=list)
    """已写入的符号路径。"""

    skipped: dict[str, str] = field(default_factory=dict)
    """跳过的符号路径 → 原因。"""

    checksums: dict[str, int] = field(default_factory=dict)
    """重算出的校验和。为空表示没有规则（也就没算）。"""

    output: Path | None = None
    """实际写入的文件。"""

    warnings: list[str] = field(default_factory=list)


def run_writeback(
    registry: PluginRegistry,
    sources: WritebackSources,
    out: Path | None = None,
    checksums: Sequence[ChecksumSpec] = (),
    progress: Callable[[str], None] | None = None,
) -> WritebackOutcome:
    """端到端写回。**任一步失败即停**，抛 `WritebackError`。

    `out` 为 None 时原地写回（原子替换）；给了就写副本，原文件不动。
    """
    ctx = JobContext(job_id="writeback")
    result = WritebackOutcome()

    def say(step: str) -> None:
        if progress is not None:
            progress(step)

    # 1. 镜像 —— 写回的底盘，字节以它为准
    say(f"解析镜像 {sources.image.name}")
    image, image_warnings = _load_image(registry, sources.image, ctx)
    result.warnings.extend(f"{sources.image.name}: {w}" for w in image_warnings)
    raw = _read_bytes(sources.image)

    # 2. 描述文件 —— 提供地址与类型
    say(f"解析描述 {sources.a2l.name}")
    model = _load_model(registry, sources.a2l, ctx)

    # 3. 值 —— 没有值集就无事可做（不猜默认值）
    if sources.values is None:
        raise WritebackError("没有给出值集（--values），不知道要把什么值写进去")
    say(f"解析值集 {sources.values.name}")
    valueset = _load_values(registry, sources.values, ctx)
    model, binding = bind_values(model, valueset)
    result.warnings.extend(f"值集绑定：{w}" for w in binding.warnings)

    # 4. 写值 —— 只写有值的量，其余逐字节搬运
    say("写值")
    applied = apply_values(model, image, byte_order=sources.byte_order)
    result.written = applied.written
    result.skipped = applied.skipped
    if not applied.written:
        raise WritebackError(
            "一个量都没写进去（"
            + "；".join(f"{k}: {v}" for k, v in list(applied.skipped.items())[:3])
            + "）—— 不产出一个其实没改过的文件"
        )

    # 5. 校验和 —— 必须在序列化之前算
    say("重算校验和")
    image.checksums = list(checksums)
    try:
        result.checksums = image.recompute_checksums()
    except ChecksumError as exc:
        raise WritebackError(f"校验和：{exc}") from exc

    # 6. 序列化 —— 格式知识在插件里，行尾沿用原文件
    say("序列化")
    writer = _writer_for(registry, kind_from_suffix(sources.image))
    blob = writer.serialize(
        image,
        {
            "line_ending": _line_ending_of(raw),
            # 原文件字节交给插件：S-Record 的 S0 头文本与终止记录类型
            # 不在内存镜像模型里，只能从原文件还原。服务层只是传递字节，
            # 不认识任何格式 —— 格式知识仍然留在插件内。
            "source_bytes": raw,
        },
    )

    # 7. 落盘 —— 最后一步，且是原子的
    target = out if out is not None else sources.image
    say(f"写入 {target.name}")
    try:
        FileSource(target).write_all(blob)
    except Exception as exc:  # noqa: BLE001 - 落盘失败要带上步骤上下文
        raise WritebackError(f"写入 {target} 失败：{exc}") from exc

    result.output = target
    return result


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _require(path: Path) -> None:
    """文件必须在。缺失要报成 `WritebackError`，好让上层能统一处理。"""
    if not path.exists():
        raise WritebackError(f"文件不存在：{path}")


def _load_image(
    registry: PluginRegistry, path: Path, ctx: JobContext
) -> tuple[AddressSpace, list[str]]:
    _require(path)
    parsers = registry.parsers_for(kind_from_suffix(path))
    if not parsers:
        raise WritebackError(f"没有能处理 {path.name} 的解析器插件")
    outcome = parsers[0].instance.parse(FileSource(path), ctx)
    if not isinstance(outcome.document, AddressSpace):
        raise WritebackError(
            f"{path.name} 没有解析出内存镜像（得到 {type(outcome.document).__name__}）"
        )
    return outcome.document, list(outcome.warnings)


def _load_model(registry: PluginRegistry, path: Path, ctx: JobContext) -> CalibrationModel:
    _require(path)
    parsers = registry.parsers_for(ArtifactKind.A2L)
    if not parsers:
        raise WritebackError(f"没有能处理 {path.name} 的描述文件解析器")
    outcome = parsers[0].instance.parse(FileSource(path), ctx)
    if not isinstance(outcome.document, CalibrationModel):
        raise WritebackError(f"{path.name} 没有解析出标定模型")
    return outcome.document


def _load_values(registry: PluginRegistry, path: Path, ctx: JobContext):
    _require(path)
    parsers = registry.parsers_for(kind_from_suffix(path))
    if not parsers:
        raise WritebackError(f"没有能处理 {path.name} 的值集解析器")
    outcome = parsers[0].instance.parse(FileSource(path), ctx)
    return outcome.document


def _writer_for(registry: PluginRegistry, kind: ArtifactKind):
    for plugin in registry.by_capability(PluginCapability.WRITER):
        candidate = plugin.instance
        if kind in candidate.kinds:
            return candidate
    raise WritebackError(f"没有能写出 {kind.value} 文件的 writer 插件")


def _line_ending_of(raw: bytes) -> str:
    """沿用原文件的行尾。

    行尾是文件级风格、不属于镜像内容，但写回要逐字节还原就必须一致：
    有的工具链写 CRLF，有的写 LF。改了它，diff 会显示整文件变动。
    """
    first_break = raw.find(b"\n")
    if first_break > 0 and raw[first_break - 1 : first_break] == b"\r":
        return "\r\n"
    return "\n"


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WritebackError(f"读取 {path} 失败：{exc}") from exc
