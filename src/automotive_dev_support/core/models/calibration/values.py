"""标定值集 —— 值集的装载、对齐与绑定。

## 为什么「绑定」必须是一个独立概念

A2L 只说「有哪些量、长什么样」，值集（DCM / CDFX / 参数集）只说
「当前值是多少」。两者是**分开演进**的：

- 一份 A2L 下会挂很多组值（基准版、现场版、某次试验版）；
- 值集可能比描述文件旧（少几个量），也可能新（含已被删掉的量）；
- 命名层级还经常对不上 —— A2L 里是扁平的 `Kp_Speed`，
  CDFX 里可能是 `ASAM.C.Ctrl.Kp_Speed`。

所以「把值贴到描述上」不是赋值，是**一次对齐**。对不上的部分必须被
显式报告，绝不能静默丢弃：**「贴完值了但某个量其实还是旧的」是标定
工程里最难查的一类事故**，它不会报错，只会让某次试验的结论悄悄错掉。

## 物理值与原始值

值集给的是**物理值**（工程师看到的东西），ECU 内存里存的是**原始值**，
两者由转换方法连接。值集不提供原始值时，这里用转换方法反算，
并按数据类型量化（整数类型取整）。

这里有一个必须知道的精度事实：整数类型的量，物理值会被量化到整数。
若某个量的物理值只变了 0.001，量化后原始值相同 —— **这种变化在原始值
层面看不见**。所以比对必须同时看物理值（见 diff 的 `phys_changed`），
不能只看原始值。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from automotive_dev_support.core.models.calibration.model import (
    CalibrationModel,
    Characteristic,
    DataType,
)
from automotive_dev_support.core.models.common import SymbolPath

__all__ = [
    "ValueCategory",
    "ValueEntry",
    "ValueSet",
    "ValueBinding",
    "bind_values",
]

#: 不量化的数据类型。其余（各种整数）在物理值反算原始值时取整。
_FLOAT_TYPES = frozenset({DataType.FLOAT32, DataType.FLOAT64})


class ValueCategory(StrEnum):
    """ASAM CDF / CDFX 的实例类别。

    与 A2L 的 CHARACTERISTIC 类型基本一一对应，
    多出来的几种是轴 —— 值集里轴点也是单独存放的。
    """

    VALUE = "VALUE"
    CURVE = "CURVE"
    MAP = "MAP"
    CUBOID = "CUBOID"
    VAL_BLK = "VAL_BLK"
    ASCII = "ASCII"
    STRUCTURE = "STRUCTURE"
    BLOB = "BLOB"

    STD_AXIS = "STD_AXIS"
    COM_AXIS = "COM_AXIS"
    RES_AXIS = "RES_AXIS"
    CURVE_AXIS = "CURVE_AXIS"
    FIX_AXIS = "FIX_AXIS"

    UNKNOWN = "UNKNOWN"

    @property
    def is_axis(self) -> bool:
        return self.value.endswith("_AXIS")


class ValueEntry(BaseModel):
    """值集里的一个量。"""

    path: SymbolPath
    category: ValueCategory = ValueCategory.UNKNOWN

    physical: list[float] = Field(default_factory=list)
    """物理值。**值集给的就是这个** —— 工程师看到与填进去的都是物理值。"""

    raw: list[float] | None = None
    """原始值。多数值集不提供，为 None 时由转换方法反算。"""

    unit: str = ""


class ValueSet(BaseModel):
    """一组标定值快照。对应 CANape CDM 里的「数据集」。"""

    name: str = ""
    source_format: str = ""
    """产出它的格式（cdfx / dcm / paramset），仅用于溯源与提示。"""

    entries: dict[str, ValueEntry] = Field(default_factory=dict)
    """键为符号路径文本。"""

    axis_values: dict[str, list[float]] = Field(default_factory=dict)
    """轴点。键为轴名，匹配模型的 `Axis.input_quantity`。"""

    warnings: list[str] = Field(default_factory=list)

    def get(self, path: str | SymbolPath) -> ValueEntry | None:
        key = path.text if isinstance(path, SymbolPath) else path
        return self.entries.get(key)

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "axes": len(self.axis_values),
            "elements": sum(len(e.physical) for e in self.entries.values()),
            "with_raw": sum(1 for e in self.entries.values() if e.raw is not None),
        }


class ValueBinding(BaseModel):
    """一次绑定的对账结果。**这份东西是给用户看的，不是给代码看的。**"""

    matched: list[str] = Field(default_factory=list)
    """精确匹配上的符号路径。"""

    matched_by_fallback: list[str] = Field(default_factory=list)
    """按末段回退匹配上的。

    命名层级不一致时的兜底（A2L 扁平、CDFX 多级）。能救急，
    但**必须报告**：同名的两个量被错配，比不匹配更危险。
    """

    missing_in_model: list[str] = Field(default_factory=list)
    """值集里有、描述里没有 —— 值集比描述新，或描述里已删掉。"""

    missing_in_values: list[str] = Field(default_factory=list)
    """描述里有、值集里没有 —— 值集比描述旧，或这次没导出。"""

    dimension_mismatch: list[dict[str, Any]] = Field(default_factory=list)
    """元素个数与描述对不上。这类量**不写值**，避免半截数据被当成完整数据。"""

    warnings: list[str] = Field(default_factory=list)

    @property
    def coverage(self) -> float:
        """覆盖率：描述里的量有多大比例拿到了值。

        回退匹配也算匹配上了 —— 它只是路径写法不同，值是真的拿到了。
        漏算它会让「其实绑得挺全」的情况显示成大面积缺失。
        """
        hit = len(self.matched) + len(self.matched_by_fallback)
        total = hit + len(self.missing_in_values)
        return hit / total if total else 1.0

    @property
    def is_clean(self) -> bool:
        return not (
            self.missing_in_model
            or self.missing_in_values
            or self.dimension_mismatch
        )


def bind_values(
    model: CalibrationModel, valueset: ValueSet
) -> tuple[CalibrationModel, ValueBinding]:
    """把值集绑到描述模型上，返回**新模型**与对账结果。

    不改输入：`Analyzer` 与加载流程都要求输入不可变，
    否则「解析一次、挂多组值」会互相污染。
    """
    bound = model.model_copy(deep=True)
    binding = ValueBinding()
    remaining = set(valueset.entries)

    # 末段索引。A2L 里符号大量扁平命名，CDFX 里常带层级，
    # 精确匹配不上时按末段回退 —— 但只在该末段**唯一**时才用，
    # 同名多个量时宁可不匹配（错配比漏配危险得多）。
    by_tail: dict[str, list[str]] = {}
    for key in valueset.entries:
        by_tail.setdefault(SymbolPath.parse(key).name, []).append(key)

    for path, char in sorted(bound.characteristics.items()):
        entry = valueset.entries.get(path)
        fell_back = False

        if entry is None:
            candidates = [k for k in by_tail.get(char.path.name, ()) if k in remaining]
            if len(candidates) == 1:
                entry = valueset.entries[candidates[0]]
                fell_back = True

        if entry is None:
            binding.missing_in_values.append(path)
            continue

        remaining.discard(entry.path.text)
        (binding.matched_by_fallback if fell_back else binding.matched).append(path)

        values = _to_raw(char, entry)
        expected = char.element_count
        if expected and len(values) != expected:
            binding.dimension_mismatch.append(
                {
                    "path": path,
                    "expected": expected,
                    "actual": len(values),
                    "from": entry.path.text,
                }
            )
            continue
        char.raw_values = values

    binding.missing_in_model = sorted(remaining)

    _bind_axes(bound, valueset, binding)
    _finalize_warnings(binding)
    return bound, binding


def _to_raw(char: Characteristic, entry: ValueEntry) -> list[float]:
    """物理值 → 原始值。值集自带原始值时直接用，否则反算并量化。"""
    if entry.raw is not None:
        return list(entry.raw)
    values = [char.compu.to_raw(p) for p in entry.physical]
    if char.data_type in _FLOAT_TYPES:
        return values
    # 整数类型：ECU 内存里存不了小数，必须量化。
    # 这一量化是不可逆的 —— 见模块文档里的精度说明。
    return [float(round(v)) for v in values]


def _bind_axes(
    model: CalibrationModel, valueset: ValueSet, binding: ValueBinding
) -> None:
    """轴点绑定。

    轴没有自己的符号名（A2L 的 AXIS_DESCR 只有输入量名），
    所以匹配依次尝试：模型里的键 → 输入量名 → 路径末段。
    """
    remaining = dict(valueset.axis_values)
    hits: dict[str, list[float]] = {}

    for key, axis in sorted(model.axes.items()):
        for candidate in (key, axis.input_quantity, axis.path.name):
            if candidate and candidate in remaining:
                hits[key] = remaining.pop(candidate)
                break

    for key, points in hits.items():
        model.axes[key].fixed_points = list(points)
    for char in model.characteristics.values():
        for axis in char.axes:
            points = hits.get(axis.path.text)
            if points is not None:
                axis.fixed_points = list(points)

    if remaining:
        binding.warnings.append(
            f"值集中有 {len(remaining)} 个轴未匹配到描述文件："
            + "、".join(sorted(remaining)[:5])
            + ("…" if len(remaining) > 5 else "")
        )


def _finalize_warnings(binding: ValueBinding) -> None:
    if binding.missing_in_model:
        preview = "、".join(binding.missing_in_model[:5])
        more = "…" if len(binding.missing_in_model) > 5 else ""
        binding.warnings.append(
            f"值集里有 {len(binding.missing_in_model)} 个量在描述文件中不存在"
            f"（值集可能比描述文件旧或命名已变）：{preview}{more}"
        )
    if binding.missing_in_values:
        preview = "、".join(sorted(binding.missing_in_values)[:5])
        more = "…" if len(binding.missing_in_values) > 5 else ""
        binding.warnings.append(
            f"描述文件里有 {len(binding.missing_in_values)} 个量没有值"
            f"（这些量保持未加载状态，不参与数值比对）：{preview}{more}"
        )
    if binding.dimension_mismatch:
        preview = "、".join(str(d["path"]) for d in binding.dimension_mismatch[:5])
        binding.warnings.append(
            f"{len(binding.dimension_mismatch)} 个量的元素个数与描述不一致，"
            f"已跳过不写值：{preview}"
        )
    if binding.matched_by_fallback:
        binding.warnings.append(
            f"{len(binding.matched_by_fallback)} 个量是靠末段同名回退匹配上的，"
            "请确认命名层级差异是有意为之"
        )
