"""Intel HEX 解析器插件。

把 `.hex` 文件装配成 `AddressSpace`（CDM 与刷写共用的原始面）。

## 与「值」的关系

HEX 只有字节，没有「哪个字节是哪个标定量」的信息。
因此本插件**不产出值集** —— 符号与地址由 A2L 描述文件提供，
两者的汇合在 `core.models.calibration.values.bind_image`。

## 三个关键决策

1. **checksum 校验失败的记录不加载。** 坏字节一旦进标定值，
   比对结果就是错的，而且看不出哪里错了。宁可缺口，不可造假。
2. **02/04 基地址按规范累积。** 段地址左移 4 位、线性地址左移 16 位，
   对后续所有数据记录生效，直到被新的基地址记录覆盖。
3. **装配交给 `ImageAssembler`。** 连续记录自动合成一段，
   重叠按「后者覆盖前者」并告警 —— 与硬件刷写行为一致。
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

__all__ = ["HexParser"]

#: Intel HEX 记录类型
_DATA = 0x00
_EOF = 0x01
_EXT_SEGMENT = 0x02
_START_SEGMENT = 0x03
_EXT_LINEAR = 0x04
_START_LINEAR = 0x05


def _parse_line(line: str, lineno: int, warnings: list[str]) -> tuple[int, int, bytes] | None:
    """解析一行，返回 (地址, 类型, 数据)。格式或校验不过返回 None。

    地址是**记录内的 16 位地址**，未叠加 02/04 基地址 ——
    基地址的累积在调用方做，因为它是跨行的状态。
    """
    body = line[1:]
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        warnings.append(f"第 {lineno} 行不是合法的十六进制文本，已跳过：{line[:40]!r}")
        return None
    if len(raw) < 5:
        warnings.append(f"第 {lineno} 行长度不足（至少需要 count/address/type/checksum），已跳过")
        return None

    count = raw[0]
    if len(raw) != 5 + count:
        warnings.append(
            f"第 {lineno} 行声明 {count} 字节数据但实际 {len(raw) - 5} 字节，已跳过"
        )
        return None

    if (sum(raw) & 0xFF) != 0:
        warnings.append(
            f"第 {lineno} 行 checksum 校验失败（和为 0x{sum(raw) & 0xFF:02X}，应为 0x00），"
            f"该记录的数据不加载"
        )
        return None

    address = int.from_bytes(raw[1:3], "big")
    rtype = raw[3]
    return address, rtype, raw[4 : 4 + count]


class HexParser:
    """`core.ports.Parser` 的实现。"""

    kinds = frozenset({ArtifactKind.HEX})

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        artifact = Artifact(
            kind=ArtifactKind.HEX,
            role=ArtifactRole.IMAGE,
            ref=source.artifact_ref(),
            producer="parser-hex",
        )

        assembler = ImageAssembler()
        warnings: list[str] = []
        segment_base = 0
        linear_base = 0
        saw_eof = False
        records = 0
        skipped = 0

        text = source.read_all().decode("ascii", errors="replace")
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                warnings.append(f"第 {lineno} 行不是 Intel HEX 记录（缺少 ':' 起始符），已跳过")
                skipped += 1
                continue

            parsed = _parse_line(line, lineno, warnings)
            if parsed is None:
                skipped += 1
                continue

            address, rtype, data = parsed
            if rtype == _DATA:
                effective = linear_base + segment_base + address
                assembler.add(effective, data, origin=f"line {lineno}")
                records += 1
            elif rtype == _EOF:
                saw_eof = True
                break
            elif rtype == _EXT_SEGMENT:
                segment_base = int.from_bytes(data[:2], "big") << 4
            elif rtype == _EXT_LINEAR:
                linear_base = int.from_bytes(data[:2], "big") << 16
            elif rtype in (_START_SEGMENT, _START_LINEAR):
                pass  # 启动地址记录对本软件无意义
            else:
                warnings.append(f"第 {lineno} 行是未知记录类型 0x{rtype:02X}，已跳过")
                skipped += 1

        if not saw_eof:
            warnings.append("文件没有 EOF 记录（01）—— 可能被截断，末尾数据未经验证")

        space: AddressSpace = assembler.build()
        warnings.extend(assembler.warnings)

        coverage = space.coverage()
        artifact.label = _stem(source.uri)
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
