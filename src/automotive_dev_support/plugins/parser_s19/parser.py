"""Motorola S-Record 解析器插件。

把 `.s19` / `.srec` / `.mot` 文件装配成 `AddressSpace`。

## 与 HEX 的分工

两者产出同一个东西（内存镜像），但记录格式毫无共性：
Intel HEX 的 checksum 是**二补数**（和为 0x00），
S-Record 的是**一补数**（和为 0xFF）。所以各自一个插件，
共用 `ImageAssembler` 装配 —— 这正是「新增格式 = 新增一个文件夹」
的日常形态：共享逻辑在 core，格式细节在插件。

## 记录类型

    S0  头记录（厂商信息文本，取出作标签）
    S1/S2/S3  数据记录，地址宽度分别为 16/24/32 位
    S5/S6  已写数据记录计数（16/24 位），仅校验用，忽略
    S7/S8/S9  结束记录，启动地址宽度 32/24/16 位
"""

from __future__ import annotations

from automotive_dev_support.core.models.memory import AddressSpace, ImageAssembler
from automotive_dev_support.core.ports.plugin import ParseOutcome
from automotive_dev_support.core.skeleton import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    DataSource,
    JobContext,
)

__all__ = ["S19Parser"]

#: 数据记录类型 → 地址宽度（字节）。键是**字符串** —— 记录类型字符原样传入。
_DATA_WIDTHS = {"1": 2, "2": 3, "3": 4}


def _parse_record(
    line: str, lineno: int, warnings: list[str]
) -> tuple[str, int, bytes] | None:
    """解析一条记录，返回 (类型字符, 地址, 数据)。失败返回 None。"""
    kind = line[1].upper()
    body = line[2:]
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        warnings.append(f"第 {lineno} 行不是合法的十六进制文本，已跳过：{line[:40]!r}")
        return None
    if len(raw) < 3:
        warnings.append(f"第 {lineno} 行长度不足，已跳过")
        return None

    count = raw[0]
    if len(raw) != count + 1:
        warnings.append(
            f"第 {lineno} 行声明 {count} 字节但实际 {len(raw) - 1} 字节，已跳过"
        )
        return None

    # S-Record checksum 是一补数：count + address + data + checksum 的和
    # 低字节应为 0xFF。
    if (sum(raw) & 0xFF) != 0xFF:
        warnings.append(
            f"第 {lineno} 行 checksum 校验失败（和为 0x{sum(raw) & 0xFF:02X}，应为 0xFF），"
            f"该记录的数据不加载"
        )
        return None

    width = _DATA_WIDTHS.get(kind, 2)
    return kind, int.from_bytes(raw[1 : 1 + width], "big"), raw[1 + width : -1]


class S19Parser:
    """`core.ports.Parser` 的实现。"""

    kinds = frozenset({ArtifactKind.S19})

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        artifact = Artifact(
            kind=ArtifactKind.S19,
            role=ArtifactRole.IMAGE,
            ref=source.artifact_ref(),
            producer="parser-s19",
        )

        assembler = ImageAssembler()
        warnings: list[str] = []
        header = ""
        saw_end = False
        records = 0
        skipped = 0

        text = source.read_all().decode("ascii", errors="replace")
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            if len(line) < 2 or line[0] not in "Ss":
                warnings.append(f"第 {lineno} 行不是 S-Record（缺少 'S' 起始符），已跳过")
                skipped += 1
                continue

            parsed = _parse_record(line, lineno, warnings)
            if parsed is None:
                skipped += 1
                continue

            kind, address, data = parsed
            if kind in "123":
                assembler.add(address, data, origin=f"line {lineno}")
                records += 1
            elif kind == "0":
                header = data.decode("ascii", errors="replace")
            elif kind in "789":
                saw_end = True
                break
            elif kind in "56":
                pass  # 计数记录，无数据
            else:
                warnings.append(f"第 {lineno} 行是未知记录类型 S{kind}，已跳过")
                skipped += 1

        if not saw_end:
            warnings.append("文件没有结束记录（S7/S8/S9）—— 可能被截断，末尾数据未经验证")

        space: AddressSpace = assembler.build()
        warnings.extend(assembler.warnings)

        coverage = space.coverage()
        artifact.label = header.strip() or _stem(source.uri)
        artifact.meta = {
            "records": records,
            "skipped": skipped,
            "segments": coverage["segments"],
            "bytes": coverage["bytes"],
            "start": coverage["start"],
            "end": coverage["end"],
            "gaps": len(coverage["gaps"]),
        }
        return ParseOutcome(artifact=artifact, document=space, warnings=warnings)


def _stem(uri: str) -> str:
    tail = uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail
