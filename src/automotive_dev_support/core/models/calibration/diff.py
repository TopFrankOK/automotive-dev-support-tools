"""标定比对的结果模型。

**为什么这个类型放在 `core` 而不是放在比对插件里？**

因为比对插件（产出方）和导出插件、视图插件（消费方）之间**禁止互相
import**（见 .importlinter 契约 3）。如果 `ChangeSet` 定义在
`plugins/diff_calibration` 里，那么导出 Excel 的插件就必须 import 它 ——
两个插件就被绑死了，以后改比对结果的结构会连带改导出插件。

把结果类型上提到 `core.models.calibration` 之后：
    plugins/diff_calibration   产出 ChangeSet
    plugins/export-excel-diff  消费 ChangeSet
    plugins/view-diff-table    消费 ChangeSet
三者互不认识，只认识 core 里的这个类型。

**这就是「插件之间禁止互相 import」的落地方式** ——
不是靠自律，而是靠把共享类型放到正确的位置。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from automotive_dev_support.core.models.calibration.model import Characteristic

__all__ = [
    "ChangeKind",
    "FieldChange",
    "ValueChange",
    "CharacteristicChange",
    "ChangeSet",
]


class ChangeKind(StrEnum):
    ADDED = "added"
    """新版本里新增（旧版本没有）。"""

    REMOVED = "removed"
    """旧版本有、新版本删掉了。"""

    VALUES_CHANGED = "values_changed"
    """数值变了。"""

    ATTRS_CHANGED = "attrs_changed"
    """属性变了（数据类型、转换方法、限值等）。"""

    ADDRESS_MOVED = "address_moved"
    """地址变了但语义没变 —— **单独列出来，不计入「改动」。**

    这一项是跨版本比对的关键。App 重编一次，成百上千个标定量的
    地址会整体漂移。如果把它算成「改动」，真正的修改会被淹没，
    报告也就没人看了。所以地址变化单独成类，
    默认在界面上折叠起来。
    """


class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None

    @property
    def display(self) -> str:
        return f"{self.field}: {self.before} → {self.after}"


class ValueChange(BaseModel):
    """单个元素的变化。曲线/图表按元素下标逐一记录。"""

    index: int
    row: int | None = None
    col: int | None = None
    raw_before: float | None = None
    raw_after: float | None = None
    phys_before: float | None = None
    phys_after: float | None = None

    @property
    def delta_physical(self) -> float | None:
        if self.phys_before is None or self.phys_after is None:
            return None
        return self.phys_after - self.phys_before

    @property
    def relative_change(self) -> float | None:
        delta = self.delta_physical
        if delta is None or not self.phys_before:
            return None
        return delta / self.phys_before


class CharacteristicChange(BaseModel):
    path: str
    name: str = ""
    kinds: list[ChangeKind] = Field(default_factory=list)
    attribute_changes: list[FieldChange] = Field(default_factory=list)
    value_changes: list[ValueChange] = Field(default_factory=list)
    address_before: int | None = None
    address_after: int | None = None
    group: str = ""

    @property
    def is_meaningful(self) -> bool:
        """是否属于「真正需要人看」的改动（地址漂移不算）。"""
        return bool(
            set(self.kinds)
            & {ChangeKind.ADDED, ChangeKind.REMOVED, ChangeKind.VALUES_CHANGED,
               ChangeKind.ATTRS_CHANGED}
        )

    @property
    def value_count(self) -> int:
        return len(self.value_changes)


class ChangeSet(BaseModel):
    """两个版本之间的完整差异。"""

    baseline_label: str = ""
    candidate_label: str = ""

    added: list[CharacteristicChange] = Field(default_factory=list)
    removed: list[CharacteristicChange] = Field(default_factory=list)
    changed: list[CharacteristicChange] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def meaningful_changes(self) -> list[CharacteristicChange]:
        """排除纯地址漂移后的改动列表。报告与界面默认展示这一份。"""
        return [c for c in (*self.added, *self.removed, *self.changed) if c.is_meaningful]

    def address_moves(self) -> list[CharacteristicChange]:
        return [
            c
            for c in self.changed
            if c.kinds == [ChangeKind.ADDRESS_MOVED]
        ]

    def summary(self) -> dict[str, Any]:
        meaningful = self.meaningful_changes()
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
            "address_moved": len(self.address_moves()),
            "meaningful": len(meaningful),
            "value_changes": sum(c.value_count for c in self.changed),
        }


def describe(characteristic: Characteristic) -> dict[str, Any]:
    """把标定量摘要成可比较的字典。

    比对插件与其它需要「属性快照」的地方共用它，
    避免两处各自实现导致字段口径不一致。
    """
    return {
        "type": str(characteristic.type),
        "data_type": str(characteristic.data_type),
        "compu": characteristic.compu.name,
        "compu_category": characteristic.compu.category,
        "unit": characteristic.compu.unit,
        "dims": list(characteristic.dims),
        "lower": characteristic.limits.lower,
        "upper": characteristic.limits.upper,
        "read_only": characteristic.read_only,
        "group": characteristic.group,
    }
