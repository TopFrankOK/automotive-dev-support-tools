"""Motorola S-Record 序列化（写回）插件。

## 与 HEX writer 是同一块料，但有一个致命差异

**checksum 是一补数**（`0xFF - (sum & 0xFF)`），
不是 Intel HEX 的二补数（`(~sum + 1) & 0xFF`）。

照抄的后果很阴：文件能写出来、能被别的工具读，
刷进 ECU 却被拒 —— 而且全程不报错，只能到了台架上才发现。
两套格式各自一个插件、共享 core，正是为了让这类差异各写各的、互不影响。

## 两样东西模型里没有，必须还回去

`AddressSpace` 只有地址与字节，而 S-Record 有：

  - **S0 头记录**：厂商文本标签（样例里是 `DEMO ENGINE MODEL`）
  - **终止记录类型与启动地址**：S7（32 位）/ S8（24 位）/ S9（16 位）

HEX 没有这个麻烦 —— 它的扩展线性地址记录能从地址本身推出来，
所以 HEX 能字节级还原。S19 若不管这两样，一写回就丢掉第一行，
`git diff` 立刻变红，"无损"就成了空话。

所以本插件从 `ctx["source_bytes"]`（原文件字节）里把它们读回来。
**不认识原文件就不编**：编一个头出来是造假，比没有更糟 ——
使用者会以为那是真的厂商信息。

## 分工

行怎么切由 core 的 `iter_blocks()` 决定，本插件只编码文本行；
仅当段没有记录边界时才按格式上限切（32 字节，多数工具的惯例）。
"""

from __future__ import annotations

from automotive_dev_support.core.models.memory import AddressSpace
from automotive_dev_support.core.skeleton.artifact import ArtifactKind

__all__ = ["S19Writer"]

#: 地址宽度（字节）→ 数据记录类型字符。与 parser 的 `_DATA_WIDTHS` 反向对称。
_DATA_KINDS = {2: "1", 3: "2", 4: "3"}

#: 终止记录类型 → 启动地址宽度（字节）。
_TERM_WIDTHS = {"7": 4, "8": 3, "9": 2}

_MAX_DATA = 0xFA
"""一行数据字节数的硬上限。

`count` 字段只有 1 字节：count = 地址宽 + 数据 + 1 ≤ 0xFF，
所以 32 位地址时数据最多 0xFA 字节。超过就会溢出，且溢出后
checksum 依然"算得出来" —— 又是一个静默错误。
"""

_DEFAULT_LINE = 32
"""没有记录边界时的默认行宽。多数工具链的惯例值。"""


class S19Writer:
    """`core.ports.Writer` 的实现：内存镜像 → S-Record 文本。"""

    writer_id = "s19"
    kinds = frozenset({ArtifactKind.S19})

    def serialize(self, image: AddressSpace, ctx: dict[str, object]) -> bytes:
        """产出完整文件的字节（含终止记录）。

        `ctx` 可选键：
          - `source_bytes`：原文件字节。用于还原模型里没有的东西 ——
            S0 头文本、终止记录类型、启动地址、行尾。
          - `header`：显式指定 S0 文本（覆盖从 `source_bytes` 读到的）。
          - `terminator`：`"7"` / `"8"` / `"9"`，默认依原文件、再兜底 S7。
          - `start_address`：终止记录里的启动地址，默认依原文件、再兜底 0。
          - `line_ending`：行尾，默认依原文件、再兜底 `"\\n"`。
          - `max_bytes_per_line`：无记录边界时的行宽上限，默认 32。
        """
        options = ctx or {}
        limit = options.get("max_bytes_per_line", _DEFAULT_LINE)
        max_bytes = int(limit) if isinstance(limit, int) else _DEFAULT_LINE
        max_bytes = max(1, min(max_bytes, _MAX_DATA))

        raw = options.get("source_bytes")
        raw = bytes(raw) if isinstance(raw, (bytes, bytearray)) else None
        scanned = _scan_source(raw)

        header = str(options["header"]) if "header" in options else scanned.header
        terminator = str(options.get("terminator") or scanned.terminator or "7")
        if terminator not in _TERM_WIDTHS:
            terminator = "7"
        start = int(options.get("start_address") or scanned.start_address or 0)
        ending = str(options.get("line_ending") or scanned.line_ending or "\n")

        lines: list[str] = []
        if header:
            lines.append(_record("0", 0x0000, header.encode("ascii", "replace"), 2))

        for address, blob in image.iter_blocks():
            offset = 0
            while offset < len(blob):
                here = address + offset
                width = _width_for(here)
                space = 1 << (8 * width)
                # 地址字段放不下的部分必须另起一行 —— 截断地址是静默的数据错位
                room = space - (here % space)
                size = min(max_bytes, len(blob) - offset, room)
                lines.append(
                    _record(
                        _DATA_KINDS[width],
                        here % space,
                        blob[offset : offset + size],
                        width,
                    )
                )
                offset += size

        term_width = _TERM_WIDTHS[terminator]
        lines.append(
            _record(terminator, start % (1 << (8 * term_width)), b"", term_width)
        )
        return "".join(f"{line}{ending}" for line in lines).encode("ascii")


def _record(kind: str, address: int, data: bytes, width: int) -> str:
    """组装一行：`S` + 类型 + count + 地址 + 数据 + checksum。"""
    body = bytes([width + len(data) + 1]) + address.to_bytes(width, "big") + data
    checksum = (0xFF - (sum(body) & 0xFF)) & 0xFF  # 一补数，不是 HEX 的二补数
    return f"S{kind}" + (body + bytes([checksum])).hex().upper()


def _width_for(address: int) -> int:
    """按地址大小选最小够用的地址宽度：16 位 S1 / 24 位 S2 / 32 位 S3。"""
    if address <= 0xFFFF:
        return 2
    if address <= 0xFFFFFF:
        return 3
    return 4


class _SourceStyle:
    """从原文件里读回的那些「模型之外的东西」。"""

    __slots__ = ("header", "terminator", "start_address", "line_ending")

    def __init__(
        self,
        header: str = "",
        terminator: str | None = None,
        start_address: int = 0,
        line_ending: str | None = None,
    ) -> None:
        self.header = header
        self.terminator = terminator
        self.start_address = start_address
        self.line_ending = line_ending


def _scan_source(raw: bytes | None) -> _SourceStyle:
    """从原文件字节里挑出写回需要、但 `AddressSpace` 里没有的信息。

    **为什么由插件来读**：头记录是 S-Record 专有的，
    终止记录类型也是。这份知识必须留在插件里 ——
    挪到服务层，就等于主程序开始认识某种文件格式了。

    只认校验通过的行：坏文件不该污染写回的产出。
    """
    if not raw:
        return _SourceStyle()

    try:
        text = raw.decode("ascii", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        return _SourceStyle()

    style = _SourceStyle(line_ending=_line_ending_of(raw))
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 2 or line[0] not in "Ss":
            continue
        kind = line[1]
        try:
            body = bytes.fromhex(line[2:])
        except ValueError:
            continue
        if len(body) < 3 or body[0] != len(body) - 1:
            continue
        if (sum(body) & 0xFF) != 0xFF:  # 校验不过的行不采信
            continue

        if kind == "0":
            style.header = body[3:-1].decode("ascii", errors="replace")
        elif kind in _TERM_WIDTHS:
            width = _TERM_WIDTHS[kind]
            style.terminator = kind
            style.start_address = int.from_bytes(body[1 : 1 + width], "big")
            break  # 终止记录之后不再是数据

    return style


def _line_ending_of(raw: bytes) -> str:
    index = raw.find(b"\n")
    if index < 0:
        return "\n"
    if index > 0 and raw[index - 1 : index] == b"\r":
        return "\r\n"
    return "\n"
