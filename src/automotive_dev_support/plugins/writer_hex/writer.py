"""Intel HEX 序列化（写回）插件。

## 它做的不是「导出」，是「还原」

写回的目的不是产出**一个能用的 hex**，而是产出**一个与原文件只差在改动处的 hex**。
这两者的区别很大：前者重新排版一遍就行，后者要求
未改动的字节逐字节相同、未改动的行逐字符相同。

做不到后者，`git diff` 会显示整文件改动 —— 改了哪个量、改了多少就看不出来了。
标定文件是要进版本库、要过评审的，那种"diff 全是红的"等于毁掉可审计性。

所以行怎么切**不由本插件决定**：core 的 `iter_blocks()` 按原文件的记录边界给块，
本插件只负责把块编码成一行文本。只有在没有边界信息时（段是程序构造的），
才由本插件按格式上限 0xFF 字节切。

## checksum 是二补数

`(~sum + 1) & 0xFF`，使整行所有字节（含 checksum 自身）之和为 0。
**不要照抄 S-Record**：那个是一补数。这是两套格式最容易互相抄错的地方。
"""

from __future__ import annotations

from automotive_dev_support.core.models.memory import AddressSpace
from automotive_dev_support.core.skeleton.artifact import ArtifactKind

__all__ = ["HexWriter"]

_DATA = 0x00
_EOF = 0x01
_EXT_LINEAR = 0x04

_MAX_DATA = 0xFF
"""一行最多 0xFF 字节数据 —— Intel HEX 规范的上限。"""


class HexWriter:
    """`core.ports.Writer` 的实现：内存镜像 → Intel HEX 文本。"""

    writer_id = "hex"
    kinds = frozenset({ArtifactKind.HEX})

    def serialize(self, image: AddressSpace, ctx: dict[str, object]) -> bytes:
        """产出完整文件的字节（含 EOF 记录）。

        `ctx` 可选键：
          - `max_bytes_per_line`：每行数据字节数上限，默认 0xFF（规范上限）。
            只有在段没有记录边界时才起作用 —— 有边界时原文件布局优先。
          - `line_ending`：行尾，默认 `"\\n"`。

        **为什么行尾要外面传**：它是文件级风格，不属于镜像内容，
        `AddressSpace` 里没有它（也不该有）。而写回要逐字节还原的话，
        行尾就必须和原文件一致 —— 有的工具链写 CRLF，有的写 LF。
        所以由调用方从原文件探测后传入；不传就用 LF。
        """
        limit = ctx.get("max_bytes_per_line", _MAX_DATA)
        max_bytes = int(limit) if isinstance(limit, int) else _MAX_DATA
        max_bytes = max(1, min(max_bytes, _MAX_DATA))
        ending = "\r\n" if ctx.get("line_ending") == "\r\n" else "\n"

        lines: list[str] = []
        base: int | None = None

        for address, blob in image.iter_blocks():
            for offset in range(0, len(blob), max_bytes):
                chunk = blob[offset : offset + max_bytes]
                chunk_address = address + offset
                upper = chunk_address >> 16
                if upper != base:
                    # 跨 64K 边界必须先声明新的线性基地址，否则地址会被截断
                    lines.append(_record(_EXT_LINEAR, 0x0000, upper.to_bytes(2, "big")))
                    base = upper
                lines.append(_record(_DATA, chunk_address & 0xFFFF, chunk))

        lines.append(_record(_EOF, 0x0000, b""))
        return "".join(f"{line}{ending}" for line in lines).encode("ascii")


def _record(rtype: int, address: int, data: bytes) -> str:
    """组装一行：`:` + 长度 + 地址 + 类型 + 数据 + checksum。"""
    body = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, rtype]) + data
    checksum = (~sum(body) + 1) & 0xFF  # 二补数：整行字节和为 0
    return ":" + (body + bytes([checksum])).hex().upper()
