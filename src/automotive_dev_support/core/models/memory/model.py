"""内存镜像领域模型。

**这是 CDM 与刷写共用的那一块。** 两个功能域看起来毫不相干，
但它们处理的是同一个东西：一段带地址的字节。

    CDM  ：把标定量的值写进镜像的对应地址，再更新校验和，然后存回文件
    刷写 ：把镜像按段依次写进 ECU 的 Flash

因此本模型是刻意「不完整」的 —— 它不认识标定量，也不认识 UDS。
标定量属于 `models.calibration`，UDS 属于 `models.flash`。
两边各自通过地址与本模型交互，彼此不知道对方存在。

## 双面设计

`Segment.data` 是**原始面**：字节原样保留，包括所有我们看不懂的部分。
语义面（标定量、符号）以「路径 + 地址 + 长度」的形式引用原始面。

写回时的正确做法是：
    1. 从原始面复制一份字节
    2. 只把被改动的地址区间重新编码
    3. 其余字节逐字节搬运

这就是「无损往返」的全部含义，也是产出的 hex 能被工具链接受的前提。
"""

from __future__ import annotations

import base64
import struct
import zlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from automotive_dev_support.core.models.common import SymbolPath, now_utc

__all__ = [
    "SegmentKind",
    "Segment",
    "SymbolKind",
    "Symbol",
    "ChecksumKind",
    "ChecksumSpec",
    "AddressSpace",
]


class SegmentKind(StrEnum):
    CODE = "code"
    DATA = "data"
    CALIBRATION = "calibration"
    BOOT = "boot"
    UNKNOWN = "unknown"


class Segment(BaseModel):
    """一段连续的地址区间。原始面的最小单位。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    start: int
    """起始地址。"""

    data: bytes = b""
    kind: SegmentKind = SegmentKind.UNKNOWN

    origin: str = ""
    """来自哪个文件、哪一段。写回时的溯源依据。"""

    @property
    def end(self) -> int:
        """末地址（不含）。"""
        return self.start + len(self.data)

    @property
    def size(self) -> int:
        return len(self.data)

    def contains(self, address: int) -> bool:
        return self.start <= address < self.end

    def overlaps(self, other: Segment) -> bool:
        return self.start < other.end and other.start < self.end

    @field_serializer("data")
    def _ser_data(self, value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    @field_validator("data", mode="before")
    @classmethod
    def _de_data(cls, value: object) -> object:
        if isinstance(value, str):
            return base64.b64decode(value)
        return value


class SymbolKind(StrEnum):
    FUNCTION = "function"
    VARIABLE = "variable"
    CALIBRATION = "calibration"
    CONSTANT = "constant"
    SECTION = "section"
    UNKNOWN = "unknown"


class Symbol(BaseModel):
    """符号表中的一条。地址会随版本漂移，路径不会。"""

    path: SymbolPath
    address: int
    size: int = 0
    kind: SymbolKind = SymbolKind.UNKNOWN
    section: str = ""
    data_type: str = ""

    @property
    def end(self) -> int:
        return self.address + self.size


class ChecksumKind(StrEnum):
    NONE = "none"
    SUM8 = "sum8"
    SUM16 = "sum16"
    SUM32 = "sum32"
    XOR8 = "xor8"
    CRC32 = "crc32"


class ChecksumSpec(BaseModel):
    """校验和规则。

    **必须是一等公民。** 改完标定值不更新校验和，刷进 ECU 会直接被拒。
    很多「改了值刷不进去」的问题，根因都在这里。
    """

    name: str
    kind: ChecksumKind = ChecksumKind.SUM32

    start: int
    """被校验区域的起始地址。"""

    end: int
    """被校验区域的结束地址（不含）。"""

    store_address: int | None = None
    """校验和结果存放地址。为 None 表示由外部流程决定。"""

    init_value: int = 0
    xor_out: int = 0
    data_width: int = 1
    """参与计算的数据宽度（字节）。"""

    algorithm_ref: str = ""
    """指向具体算法实现（可能是厂商私有的）。为空时用内置算法。"""


class AddressSpace(BaseModel):
    """内存镜像：地址空间 + 段 + 符号 + 校验规则。

    刻意不提供「序列化回文件」的方法 —— 那属于导出器插件的职责。
    本模型只保证能**精确读写任意地址的字节**，格式还原由插件决定。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    segments: list[Segment] = Field(default_factory=list)
    checksums: list[ChecksumSpec] = Field(default_factory=list)
    symbols: dict[str, Symbol] = Field(default_factory=dict)
    """键为符号路径文本，便于按路径直接查找。"""

    created_at: datetime = Field(default_factory=now_utc)

    # -- 读 ----------------------------------------------------------------

    def read(self, address: int, size: int) -> bytes:
        """按地址读取。跨段时逐段拼接；空洞用 0xFF 填充（Flash 擦除态）。"""
        out = bytearray()
        cursor = address
        remaining = size
        for seg in self._sorted():
            if remaining <= 0:
                break
            if seg.end <= cursor:
                continue
            if seg.start > cursor:
                gap = min(seg.start - cursor, remaining)
                out.extend(b"\xff" * gap)
                cursor += gap
                remaining -= gap
            if seg.contains(cursor):
                take = min(seg.end - cursor, remaining)
                offset = cursor - seg.start
                out.extend(seg.data[offset : offset + take])
                cursor += take
                remaining -= take
        if remaining > 0:
            out.extend(b"\xff" * remaining)
        return bytes(out)

    def symbol_value(self, path: str) -> bytes | None:
        sym = self.symbols.get(path)
        if sym is None or sym.size == 0:
            return None
        return self.read(sym.address, sym.size)

    def covers(self, address: int, size: int) -> bool:
        """`[address, address+size)` 是否被完整覆盖。

        **读之前必须先问它。** `read()` 对空洞用 0xFF 填充
        （Flash 擦除态）—— 不检查覆盖就直接读，洞里的 0xFF
        会被当成真实数据用，而且看不出任何异常。
        """
        if size <= 0:
            return False
        end = address + size
        for seg in self._sorted():
            if seg.end <= address:
                continue
            if seg.start > address:
                return False
            if seg.end >= end:
                return True
            address = seg.end
        return False

    # -- 写 ----------------------------------------------------------------

    def write(self, address: int, data: bytes) -> None:
        """就地写入。

        只覆盖目标区间，**不清除也不重排任何其它字节** ——
        这是无损往返的核心约束。若目标区间跨越了镜像里不存在的地址，
        写入会被拒绝；需要扩展镜像时应显式调用 `add_segment`。
        """
        cursor = address
        pos = 0
        remaining = len(data)
        for seg in self._sorted():
            if remaining <= 0:
                break
            if seg.end <= cursor:
                continue
            if seg.start > cursor:
                raise ValueError(
                    f"地址 0x{cursor:X} 不在任何段内，拒绝写入"
                    f"（镜像不完整，需先加载对应段）"
                )
            take = min(seg.end - cursor, remaining)
            offset = cursor - seg.start
            seg.data = seg.data[:offset] + data[pos : pos + take] + seg.data[offset + take :]
            cursor += take
            pos += take
            remaining -= take
        if remaining > 0:
            raise ValueError(f"地址 0x{cursor:X} 之后的 {remaining} 字节无对应段")

    def add_segment(self, segment: Segment) -> None:
        self.segments.append(segment)

    # -- 校验和 -------------------------------------------------------------

    def recompute_checksums(self) -> dict[str, int]:
        """按 `checksums` 里的规则重算并写入。

        返回 `{名称: 计算值}`。**任何「修改后保存」的流程都必须调用它**，
        否则写出的文件会被刷写工具或 ECU 拒绝。
        """
        results: dict[str, int] = {}
        for spec in self.checksums:
            if spec.kind is ChecksumKind.NONE:
                continue
            blob = self.read(spec.start, spec.end - spec.start)
            value = _compute_checksum(blob, spec)
            results[spec.name] = value
            if spec.store_address is not None:
                width = max(1, spec.data_width if spec.kind is not ChecksumKind.SUM32 else 4)
                payload = value.to_bytes(width, "big", signed=False)[-width:]
                try:
                    self.write(spec.store_address, payload)
                except ValueError:
                    # 校验和存放地址不在镜像内 —— 记录但不中断，
                    # 由上层决定是警告还是失败。
                    pass
        return results

    # -- 自检 --------------------------------------------------------------

    def coverage(self) -> dict[str, object]:
        """镜像完整性概览。用来在界面上提示「这个 hex 只含一部分地址」。"""
        ordered = self._sorted()
        if not ordered:
            return {"segments": 0, "bytes": 0, "start": None, "end": None, "gaps": []}
        gaps: list[tuple[int, int]] = []
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.start > prev.end:
                gaps.append((prev.end, nxt.start))
        return {
            "segments": len(ordered),
            "bytes": sum(s.size for s in ordered),
            "start": ordered[0].start,
            "end": ordered[-1].end,
            "gaps": gaps,
        }

    def _sorted(self) -> list[Segment]:
        return sorted(self.segments, key=lambda s: s.start)


def _compute_checksum(blob: bytes, spec: ChecksumSpec) -> int:
    """内置校验算法。厂商私有算法应通过 `algorithm_ref` 走插件。"""
    match spec.kind:
        case ChecksumKind.SUM8:
            return (sum(blob) + spec.init_value) & 0xFF ^ spec.xor_out
        case ChecksumKind.SUM16:
            value = 0
            for i in range(0, len(blob) - 1, 2):
                value += struct.unpack_from(">H", blob, i)[0]
            return (value + spec.init_value) & 0xFFFF ^ spec.xor_out
        case ChecksumKind.SUM32:
            value = 0
            for i in range(0, len(blob) - 3, 4):
                value += struct.unpack_from(">I", blob, i)[0]
            return (value + spec.init_value) & 0xFFFFFFFF ^ spec.xor_out
        case ChecksumKind.XOR8:
            value = spec.init_value
            for byte in blob:
                value ^= byte
            return value & 0xFF ^ spec.xor_out
        case ChecksumKind.CRC32:
            return (zlib.crc32(blob, spec.init_value) & 0xFFFFFFFF) ^ spec.xor_out
        case _:
            return 0
