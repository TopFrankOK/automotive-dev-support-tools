"""从原始字节解码通道样本 —— 纯计算，零 IO。

MF4 的样本读取路径是：用例层按 `DataLocation` 把字节读进来
（一次 `read_at`，读整个数据区），交给本模块解码。
「读」与「解」分开是有意的：读的优化（流式、mmap、缓存）
与解的优化（向量化、numpy）可以各自演进，互不绑架。

## 只解字节对齐

位压缩信号（`bit_count % 8 != 0`）返回 `None`，由调用方告警跳过。
理由：位域的字节序语义在规范里相当绕（Motorola 还要做位偏移补偿），
解错了不会报错，只会安静地给出错数 —— 那比不解更糟。
总线记录里常见的是字节对齐通道，先把这条路走稳。
"""

from __future__ import annotations

import struct
from array import array

from automotive_dev_support.core.models.measurement.model import DataTypeSpec, ValueDtype

__all__ = ["decode_samples", "TYPECODES"]

#: dtype → 位宽 → array 模块类型码（array 是原生字节序，大端数据解完要 byteswap）
_FMT_CHARS: dict[ValueDtype, dict[int, str]] = {
    ValueDtype.UINT: {1: "B", 2: "H", 4: "I", 8: "Q"},
    ValueDtype.INT: {1: "b", 2: "h", 4: "i", 8: "q"},
    ValueDtype.FLOAT: {4: "f", 8: "d"},
}

TYPECODES: dict[tuple[ValueDtype, int], str] = {
    (dtype, size): code
    for dtype, sizes in _FMT_CHARS.items()
    for size, code in sizes.items()
}

#: (dtype, 字节数, 字节序) → 解包器。大步长场景下必须逐样本取。
_STRUCTS: dict[tuple[ValueDtype, int, str], struct.Struct] = {
    (dtype, size, order): struct.Struct(
        ("<" if order == "little" else ">") + code
    )
    for dtype, sizes in _FMT_CHARS.items()
    for size, code in sizes.items()
    for order in ("little", "big")
}


def decode_samples(
    raw: bytes,
    spec: DataTypeSpec,
    count: int,
    stride: int,
    offset: int,
) -> array | None:
    """按布局从记录流解码 `count` 个样本。

    参数
    ----
    raw     整个数据区的字节（一个 CG 的全部记录，顺序存放）
    spec    通道编码（类型 / 位宽 / 字节序）
    count   样本条数
    stride  记录步长（字节）
    offset  样本在记录内的字节偏移

    返回
    ----
    `array` 实例；位压缩或位宽不支持时返回 `None`。
    注意**调用方必须先确认 `offset + spec.byte_size <= stride`**，
    本模块信任布局参数（布局来自索引阶段读到的块字段，不是用户输入）。
    """
    if count <= 0:
        return array(TYPECODES.get((spec.dtype, spec.byte_size), "d"))
    if not spec.byte_aligned:
        return None
    size = spec.byte_size
    code = TYPECODES.get((spec.dtype, size))
    if code is None:
        return None

    big_endian = spec.byte_order == "big"

    # 快路：记录里只有这一个通道（步长 == 元素大小）—— 直接整块转换
    if stride == size:
        chunk = raw[offset : offset + size * count]
        if len(chunk) < size * count:
            return None  # 数据区被截断，宁缺不假
        out = array(code)
        out.frombytes(chunk)
        if big_endian:
            out.byteswap()
        return out

    # 慢路：多通道记录，逐样本按偏移取
    unpack = _STRUCTS[(spec.dtype, size, spec.byte_order)].unpack_from
    out = array(code)
    pos = offset
    for _ in range(count):
        out.append(unpack(raw, pos)[0])
        pos += stride
    return out
