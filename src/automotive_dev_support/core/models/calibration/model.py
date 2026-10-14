"""标定领域模型。

与 `models.memory` 的关系：本模型只描述**「有哪些标定量、它们长什么样、
当前值是多少」**，不描述字节落在哪里。字节的事由内存镜像负责。
两者的连接点是 `Characteristic.address`。

这样切分的好处很直接：`MemoryImage` 既能服务 CDM，也能服务刷写；
而 `CalibrationModel` 在不需要字节的场景（比如只做属性 diff、
或生成标定清单）里可以完全不加载镜像。

## 跨版本对齐

`characteristics` 以 **符号路径文本** 为键，地址只是普通属性。
任何跨版本比对都必须先按路径对齐，再比属性与值。
按地址对齐会产出满屏假差异。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from automotive_dev_support.core.models.common import SymbolPath

__all__ = [
    "DataType",
    "CompuMethod",
    "AxisKind",
    "Axis",
    "CharacteristicType",
    "Limits",
    "Characteristic",
    "CalibrationDataset",
    "CalibrationModel",
]


class DataType(StrEnum):
    UINT8 = "uint8"
    SINT8 = "sint8"
    UINT16 = "uint16"
    SINT16 = "sint16"
    UINT32 = "uint32"
    SINT32 = "sint32"
    UINT64 = "uint64"
    SINT64 = "sint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    ASCII = "ascii"

    @property
    def byte_size(self) -> int:
        table = {
            DataType.UINT8: 1, DataType.SINT8: 1,
            DataType.UINT16: 2, DataType.SINT16: 2,
            DataType.UINT32: 4, DataType.SINT32: 4,
            DataType.UINT64: 8, DataType.SINT64: 8,
            DataType.FLOAT32: 4, DataType.FLOAT64: 8,
        }
        return table.get(self, 1)


class CompuMethod(BaseModel):
    """原始值 ↔ 物理值 的转换（A2L 的 COMPU_METHOD）。

    这是标定的灵魂：ECU 里存的是整数，工程师看的是物理量。
    比对时必须比**物理值**，否则改了一个系数导致的整片数值变化
    会被当成「所有值都变了」。
    """

    name: str = "IDENTITY"
    category: str = "IDENTITY"
    """IDENTITY / LINEAR / RAT_FUNC / TAB_VERB / TAB_INTP / FORM"""

    #: 有理函数系数，含义随 category 而定
    coefficients: list[float] = Field(default_factory=list)
    unit: str = ""
    format: str = ""
    """显示格式，例如 "%.3f"。"""

    #: TAB_* 类型的查表点
    raw_points: list[float] = Field(default_factory=list)
    phys_points: list[float] = Field(default_factory=list)

    def to_physical(self, raw: float) -> float:
        match self.category:
            case "IDENTITY":
                return raw
            case "LINEAR":
                if len(self.coefficients) >= 2:
                    return self.coefficients[0] * raw + self.coefficients[1]
                return raw
            case "RAT_FUNC":
                c = self.coefficients
                if len(c) >= 6 and (c[3] * raw * raw + c[4] * raw + c[5]) != 0:
                    return (c[0] * raw * raw + c[1] * raw + c[2]) / (
                        c[3] * raw * raw + c[4] * raw + c[5]
                    )
                return raw
            case "TAB_INTP":
                return _interp(self.raw_points, self.phys_points, raw)
            case "TAB_VERB":
                index = int(raw)
                if 0 <= index < len(self.phys_points):
                    return self.phys_points[index]
                return raw
            case _:
                return raw

    def to_raw(self, physical: float) -> float:
        match self.category:
            case "IDENTITY":
                return physical
            case "LINEAR":
                if len(self.coefficients) >= 2 and self.coefficients[0] != 0:
                    return (physical - self.coefficients[1]) / self.coefficients[0]
                return physical
            case "TAB_INTP":
                return _interp(self.phys_points, self.raw_points, physical)
            case _:
                return physical


def _interp(xs: list[float], ys: list[float], x: float) -> float:
    if not xs or not ys or len(xs) != len(ys):
        return x
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            span = xs[i + 1] - xs[i]
            if span == 0:
                return ys[i]
            ratio = (x - xs[i]) / span
            return ys[i] + ratio * (ys[i + 1] - ys[i])
    return ys[-1]


class AxisKind(StrEnum):
    STD_AXIS = "std_axis"
    """独立轴，有自己的地址与值。"""

    COM_AXIS = "com_axis"
    """共享轴，多个 MAP 复用同一个轴。"""

    FIX_AXIS = "fix_axis"
    """固定轴，仅由描述文件定义，不占 ECU 内存。"""

    RES_AXIS = "res_axis"


class Axis(BaseModel):
    """曲线/图表的坐标轴。"""

    path: SymbolPath
    kind: AxisKind = AxisKind.STD_AXIS
    count: int = 0
    data_type: DataType = DataType.FLOAT32
    compu: CompuMethod = Field(default_factory=CompuMethod)
    input_quantity: str = ""

    address: int | None = None
    """仅 STD_AXIS / COM_AXIS 有；FIX_AXIS 为 None。"""

    fixed_points: list[float] = Field(default_factory=list)
    """FIX_AXIS 的点，或已解析出的轴值。"""

    @property
    def is_shared(self) -> bool:
        return self.kind is AxisKind.COM_AXIS


class CharacteristicType(StrEnum):
    VALUE = "value"
    CURVE = "curve"
    MAP = "map"
    CUBOID = "cuboid"
    VAL_BLK = "val_blk"
    ASCII = "ascii"


class Limits(BaseModel):
    lower: float | None = None
    upper: float | None = None

    def contains(self, value: float) -> bool:
        if self.lower is not None and value < self.lower:
            return False
        if self.upper is not None and value > self.upper:
            return False
        return True


class Characteristic(BaseModel):
    """一个标定量。"""

    path: SymbolPath
    """★ 稳定逻辑标识。跨版本对齐一律以它为准。"""

    name: str = ""
    type: CharacteristicType = CharacteristicType.VALUE
    data_type: DataType = DataType.UINT16
    compu: CompuMethod = Field(default_factory=CompuMethod)

    address: int | None = None
    """★ 只是属性。App 重编一次就可能漂移，不可作为标识。"""

    dims: list[int] = Field(default_factory=list)
    """维度。VALUE 为空；CURVE 为 [n]；MAP 为 [x, y]。"""

    axes: list[Axis] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    extended_limits: Limits = Field(default_factory=Limits)

    group: str = ""
    """所属标定组（A2L 的 GROUP）。按功能分组，便于按模块筛选。"""

    function: str = ""
    display: str = ""
    description: str = ""
    read_only: bool = False

    raw_values: list[float] | None = None
    """原始面当前值。为 None 表示「未加载值集」，只有描述信息。

    浮点容器而非整数容器，是刻意的：值集（CDFX 等）给的是物理值，
    反算出的原始值可能带小数。整数类型在绑定时已量化，
    见 `values._to_raw`。
    """

    array_size: int | None = None
    """VAL_BLK 的元素个数。"""

    # -- 便捷视图 ----------------------------------------------------------

    @property
    def element_count(self) -> int:
        if self.type is CharacteristicType.VALUE:
            return 1
        if self.type is CharacteristicType.VAL_BLK:
            return self.array_size or 0
        count = 1
        for d in self.dims:
            count *= d
        return count

    def physical_values(self) -> list[float]:
        """物理值序列。比对与显示都应该用这个，而不是 `raw_values`。"""
        if self.raw_values is None:
            return []
        return [self.compu.to_physical(float(v)) for v in self.raw_values]

    def is_value_loaded(self) -> bool:
        return self.raw_values is not None


class CalibrationDataset(BaseModel):
    """一组标定值快照 —— 对应 CANape CDM 里的「数据集」。

    CDM 真正好用的地方就在于它是**多数据集**的：
    同一个 A2L 描述下挂着若干组值（基准版、现场版、某次试验版），
    任意两组之间可以比对、可以合并、可以把某一组里的部分标定量
    挪到另一组。这个模型就是为这种工作方式准备的。
    """

    name: str
    image_artifact_id: str | None = None
    """关联的内存镜像工件 id（HEX/ELF 等）。"""

    values_artifact_id: str | None = None
    """关联的值集工件 id（DCM/CDFX/参数集）。"""

    label: str = ""
    note: str = ""
    characteristic_count: int = 0


class CalibrationModel(BaseModel):
    """一次解析会话中标定侧的完整视图。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    description_artifact_id: str | None = None
    """A2L 工件 id。"""

    characteristics: dict[str, Characteristic] = Field(default_factory=dict)
    """键为 `SymbolPath.text`。用路径索引是刻意的 —— 见模块文档。"""

    axes: dict[str, Axis] = Field(default_factory=dict)
    datasets: list[CalibrationDataset] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)
    """解析器写入的自由信息（项目名、模块列表、各类块计数等）。

     与 `Artifact.meta` 一样：**骨架不解释这里的内容**，
     因此解析器可以自由扩充，不影响任何其它模块。"""

    # -- 查询 --------------------------------------------------------------

    def get(self, path: str | SymbolPath) -> Characteristic | None:
        key = path.text if isinstance(path, SymbolPath) else path
        return self.characteristics.get(key)

    def by_group(self, group: str) -> list[Characteristic]:
        return [c for c in self.characteristics.values() if c.group == group]

    def by_prefix(self, prefix: str) -> list[Characteristic]:
        """按符号路径前缀筛选，例如 `EngCal.Ctrl`。

        匹配按**段边界**进行，同时允许最后一段是「部分前缀」。
        这一点是刻意的：A2L 里大量符号是扁平命名的（`Kp_Speed`、
        `Torque_Map`），用户输入 "Torque" 期待的是
        「所有 Torque 开头的符号」，而不是「名字恰好叫 Torque 的那一个」。
        若要求整段严格相等，这个最常用的筛选方式会直接返回空集。

        传空串返回全部（等价于「不筛选」）。
        """
        head = SymbolPath.parse(prefix)
        if not head.segments:
            return list(self.characteristics.values())
        return [
            c for c in self.characteristics.values() if _prefix_match(c.path, head)
        ]

    def with_values(self) -> list[Characteristic]:
        return [c for c in self.characteristics.values() if c.is_value_loaded()]

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for c in self.characteristics.values():
            counts[str(c.type)] = counts.get(str(c.type), 0) + 1
        return {
            "characteristics": len(self.characteristics),
            "axes": len(self.axes),
            "datasets": len(self.datasets),
            "groups": len(self.groups),
            "by_type": counts,
            "with_values": len(self.with_values()),
        }


def _prefix_match(path: SymbolPath, head: SymbolPath) -> bool:
    """路径是否落在 `head` 这棵子树下（最后一段允许是部分前缀）。

    `Kp_Speed` 对 `Kp` → 命中；`Speed_Kp` 对 `Kp` → 不命中。
    段边界只看最后一个点什么位置，不从字符串中间截断，
    这样 `EngCal.Ctrl` 不会意外命中 `EngCalX.CtrlY`。
    """
    if path.depth < head.depth:
        return False
    if path.segments[: head.depth - 1] != head.segments[:-1]:
        return False
    return path.segments[head.depth - 1].startswith(head.segments[-1])
