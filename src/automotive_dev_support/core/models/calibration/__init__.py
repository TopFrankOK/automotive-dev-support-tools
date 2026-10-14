"""标定领域 —— 标定量、轴、转换方法、数据集，以及比对结果模型。"""

from automotive_dev_support.core.models.calibration.diff import (
    ChangeKind,
    ChangeSet,
    CharacteristicChange,
    FieldChange,
    ValueChange,
    describe,
)
from automotive_dev_support.core.models.calibration.model import (
    Axis,
    AxisKind,
    CalibrationDataset,
    CalibrationModel,
    Characteristic,
    CharacteristicType,
    CompuMethod,
    DataType,
    Limits,
)
from automotive_dev_support.core.models.calibration.values import (
    ValueBinding,
    ValueCategory,
    ValueEntry,
    ValueSet,
    bind_values,
)

__all__ = [
    # 模型
    "Characteristic",
    "CharacteristicType",
    "DataType",
    "CompuMethod",
    "Limits",
    "Axis",
    "AxisKind",
    "CalibrationDataset",
    "CalibrationModel",
    # 值集
    "ValueCategory",
    "ValueEntry",
    "ValueSet",
    "ValueBinding",
    "bind_values",
    # 比对结果
    "ChangeSet",
    "ChangeKind",
    "CharacteristicChange",
    "FieldChange",
    "ValueChange",
    "describe",
]
