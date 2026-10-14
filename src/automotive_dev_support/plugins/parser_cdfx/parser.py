"""CDFX（ASAM CDF）值集解析器。

产出 `ValueSet` —— **只有值，没有结构**。结构由 A2L 描述文件提供，
两者在 `bind_values` 处汇合。这个分工是刻意的：
值集与描述文件本来就分开演进，解析阶段也不该把它们绑在一起。

## 两个容易做错的地方

**1. 实例是树形的，路径要自己拼。**
`ASAM.C.MAP` 在文件里是三层嵌套的 `SW-INSTANCE`，每层只给一个
`SHORT-NAME`。只看当前层的名字会得到三个叫 "ASAM"、"C"、"MAP" 的量，
而真正的符号是 `ASAM.C.MAP`。所以必须沿祖先链把路径拼出来。

**2. 找值不能跨进嵌套实例。**
父实例（STRUCTURE / 数组）本身没有值，但它的后代里有子实例的值。
如果直接搜后代，一个容器会凭空多出几百个值。
所以取值时遇到嵌套的 `SW-INSTANCE` 必须停住。

## 关于命名空间

ASAM CDF 允许带命名空间，但标准本身也建议不带（不少 XML 工具支持不好）。
这里一律按**本地名**匹配，两种情况都能读。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Iterator

from automotive_dev_support.core.models.calibration import (
    ValueCategory,
    ValueEntry,
    ValueSet,
)
from automotive_dev_support.core.models.common import SymbolPath
from automotive_dev_support.core.ports.plugin import ParseOutcome
from automotive_dev_support.core.skeleton import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    DataSource,
    JobContext,
)

__all__ = ["CdfxParser"]

_INSTANCE = "SW-INSTANCE"
_VALUES_PHYS = "SW-VALUES-PHYS"
_VALUES_IMPL = "SW-VALUES-IMPL"
_VALUE = "V"


def _local(tag: str) -> str:
    """去掉 XML 命名空间前缀，只留本地名。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(elem: Any, name: str) -> str:
    for child in elem:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _find_own(elem: Any, name: str, stop: str = _INSTANCE) -> Any:
    """在本实例范围内找名为 `name` 的后代，**不进入嵌套实例**。"""
    for child in elem:
        local = _local(child.tag)
        if local == stop:
            continue
        if local == name:
            return child
        found = _find_own(child, name, stop)
        if found is not None:
            return found
    return None


def _collect_numbers(
    elem: Any, stop: str = _INSTANCE, bad: list[str] | None = None
) -> list[float]:
    """按文档顺序收集所有 `<V>` 的文本。

    标量是 `<V>1</V>`，曲线与图表是 `<VG><V>…</V></VG>`，
    递归收集对两种都成立，且天然是行主序。
    """
    out: list[float] = []
    for child in elem:
        local = _local(child.tag)
        if local == stop:
            continue
        if local == _VALUE:
            text = (child.text or "").strip()
            if not text:
                continue
            try:
                out.append(float(text))
            except ValueError:
                if bad is not None:
                    bad.append(text)
        else:
            out.extend(_collect_numbers(child, stop, bad))
    return out


def _walk(elem: Any, trail: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    """遍历实例树，产出（路径段元组, 实例元素）。"""
    for child in elem:
        if _local(child.tag) == _INSTANCE:
            name = _child_text(child, "SHORT-NAME")
            path = (*trail, name) if name else trail
            yield path, child
            yield from _walk(child, path)
        else:
            yield from _walk(child, trail)


def _category(inst: Any) -> ValueCategory:
    raw = _child_text(inst, "CATEGORY").upper()
    try:
        return ValueCategory(raw)
    except ValueError:
        return ValueCategory.UNKNOWN


class CdfxParser:
    """`core.ports.Parser` 的实现。"""

    kinds = frozenset({ArtifactKind.CDFX})

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        artifact = Artifact(
            kind=ArtifactKind.CDFX,
            role=ArtifactRole.VALUES,
            ref=source.artifact_ref(),
            producer="parser-cdfx",
        )

        try:
            text = source.read_all().decode("utf-8", errors="replace")
            root = ET.fromstring(text)
        except (ET.ParseError, UnicodeError) as exc:
            return ParseOutcome(
                artifact=artifact,
                document=ValueSet(source_format="cdfx"),
                warnings=[f"CDFX 不是合法的 XML，无法解析：{exc}"],
            )

        valueset = ValueSet(
            name=_child_text(root, "SHORT-NAME") or _stem(source.uri),
            source_format="cdfx",
        )

        for segments, inst in _walk(root, ()):
            if not segments:
                continue

            container = _find_own(inst, _VALUES_PHYS)
            if container is None:
                # 容器节点（STRUCTURE / 数组）自身没有值，跳过。
                continue

            path_text = ".".join(segments)
            bad: list[str] = []
            physical = _collect_numbers(container, bad=bad)
            if bad:
                valueset.warnings.append(
                    f"{path_text} 有 {len(bad)} 个无法解析成数值的元素：{bad[:3]}"
                )

            category = _category(inst)
            if category.is_axis:
                if path_text in valueset.axis_values:
                    valueset.warnings.append(f"轴 {path_text} 重复出现，保留先出现的那个")
                    continue
                valueset.axis_values[path_text] = physical
                continue

            if path_text in valueset.entries:
                valueset.warnings.append(f"实例 {path_text} 重复出现，保留先出现的那个")
                continue

            impl = _find_own(inst, _VALUES_IMPL)
            valueset.entries[path_text] = ValueEntry(
                path=SymbolPath.parse(path_text),
                category=category,
                physical=physical,
                raw=_collect_numbers(impl) if impl is not None else None,
                unit=_child_text(inst, "UNIT"),
            )

        artifact.label = valueset.name
        artifact.meta = {
            "root_element": _local(root.tag),
            "instances": len(valueset.entries),
            "axes": len(valueset.axis_values),
        }
        return ParseOutcome(
            artifact=artifact, document=valueset, warnings=list(valueset.warnings)
        )


def _stem(uri: str) -> str:
    tail = uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail
