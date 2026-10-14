"""内存镜像领域 —— CDM 与刷写共用的那一个模型。"""

from automotive_dev_support.core.models.memory.assembly import ImageAssembler
from automotive_dev_support.core.models.memory.model import (
    AddressSpace,
    ChecksumKind,
    ChecksumSpec,
    Segment,
    SegmentKind,
    Symbol,
    SymbolKind,
)

__all__ = [
    "AddressSpace",
    "ImageAssembler",
    "Segment",
    "SegmentKind",
    "Symbol",
    "SymbolKind",
    "ChecksumSpec",
    "ChecksumKind",
]
