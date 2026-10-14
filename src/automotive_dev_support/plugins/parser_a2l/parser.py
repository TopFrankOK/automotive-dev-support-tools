"""A2L 解析器插件。

把一个 A2L 描述文件映射成 `CalibrationModel`。

## 这个插件在验证什么

它是「新增一种文件格式 = 新增一个文件夹」这句话的实证：
本文件只 import `automotive_dev_support.core`，不知道 platform 的存在，
不知道界面长什么样，也不知道谁在调用它。
删掉这个文件夹，软件其余部分照常运行 —— 只是不再支持 A2L。

## 关于「无损」

A2L 是**只读**的描述文件，我们不改写它，所以这里不需要
双面模型那套保真的讲究。真正需要保真的是 HEX / ELF / DCM，
那些由对应的解析器插件负责。

## 映射的关键决策

`characteristics` 以**符号路径文本**为键，而不是列表、也不是地址。
这是全系统跨版本对齐的基础：App 重编一次地址就漂移，
按地址索引会让之后的每一次比对都产出满屏假差异。
"""

from __future__ import annotations

from typing import Any

from automotive_dev_support.core.models.calibration import (
    Axis,
    AxisKind,
    CalibrationModel,
    Characteristic,
    CharacteristicType,
    CompuMethod,
    DataType,
    Limits,
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

from automotive_dev_support.plugins.parser_a2l.a2l_syntax import (
    Block,
    parse_blocks,
    tokenize,
    unquote,
)

__all__ = ["A2lParser"]

#: 常见 RECORD_LAYOUT 命名 → 数据类型。A2L 的类型信息藏在
#: RECORD_LAYOUT 里，而绝大多数文件的命名都遵循这套约定。
_DATATYPE_HINTS: list[tuple[str, DataType]] = [
    ("FLOAT64", DataType.FLOAT64),
    ("FLOAT32", DataType.FLOAT32),
    ("A_UINT64", DataType.UINT64),
    ("A_INT64", DataType.SINT64),
    ("ULONG", DataType.UINT32),
    ("SLONG", DataType.SINT32),
    ("UWORD", DataType.UINT16),
    ("SWORD", DataType.SINT16),
    ("UBYTE", DataType.UINT8),
    ("SBYTE", DataType.SINT8),
    ("LONG", DataType.UINT32),
    ("WORD", DataType.UINT16),
    ("BYTE", DataType.UINT8),
]

_AXIS_KIND_BY_TOKEN = {
    "STD_AXIS": AxisKind.STD_AXIS,
    "COM_AXIS": AxisKind.COM_AXIS,
    "FIX_AXIS": AxisKind.FIX_AXIS,
    "RES_AXIS": AxisKind.RES_AXIS,
}

_CHAR_TYPE_BY_TOKEN = {
    "VALUE": CharacteristicType.VALUE,
    "CURVE": CharacteristicType.CURVE,
    "MAP": CharacteristicType.MAP,
    "CUBOID": CharacteristicType.CUBOID,
    "VAL_BLK": CharacteristicType.VAL_BLK,
    "ASCII": CharacteristicType.ASCII,
}


def _to_int(token: str | None) -> int | None:
    if token is None:
        return None
    try:
        return int(token, 0)
    except ValueError:
        try:
            return int(float(token))
        except ValueError:
            return None


def _to_float(token: str | None) -> float | None:
    if token is None:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _guess_data_type(record_layout: str, char_type: CharacteristicType) -> DataType:
    upper = record_layout.upper()
    for needle, dtype in _DATATYPE_HINTS:
        if needle in upper:
            return dtype
    return DataType.FLOAT32 if char_type is not CharacteristicType.ASCII else DataType.ASCII


class A2lParser:
    """`core.ports.Parser` 的实现。"""

    kinds: frozenset[ArtifactKind] = frozenset({ArtifactKind.A2L})

    # -- 入口 --------------------------------------------------------------

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        ctx.report(0, 100, "读取 A2L")
        raw = source.read_all()
        text = raw.decode("utf-8", errors="replace")

        ctx.report(20, 100, "分词")
        tokens = tokenize(text)

        ctx.report(40, 100, "解析块结构")
        root = parse_blocks(tokens)

        ctx.report(60, 100, "提取转换方法")
        compus = self._parse_compu_methods(root)

        ctx.report(75, 100, "提取标定量")
        model = CalibrationModel()
        self._fill_characteristics(root, compus, model)
        self._fill_axes(root, compus, model)
        self._fill_groups(root, model)
        self._fill_metadata(root, model)

        ctx.report(100, 100, f"完成，共 {len(model.characteristics)} 个标定量")

        artifact = Artifact(
            kind=ArtifactKind.A2L,
            role=ArtifactRole.DESCRIPTION,
            ref=source.artifact_ref(),
            producer="parser-a2l",
            label=model.metadata.get("project", ""),
            meta={"stats": model.stats()},
        )
        return ParseOutcome(
            artifact=artifact,
            document=model,
            warnings=list(model.warnings),
        )

    # -- COMPU_METHOD ------------------------------------------------------

    def _parse_compu_methods(self, root: Block) -> dict[str, CompuMethod]:
        result: dict[str, CompuMethod] = {}
        for block in root.find_all("COMPU_METHOD"):
            scalars = block.scalars
            if len(scalars) < 5:
                continue
            method = CompuMethod(
                name=unquote(scalars[0]),
                category=scalars[2].upper(),
                format=unquote(scalars[3]),
                unit=unquote(scalars[4]),
            )
            self._fill_compu_coefficients(block, method)
            result[method.name] = method
        return result

    @staticmethod
    def _fill_compu_coefficients(block: Block, method: CompuMethod) -> None:
        if linear := block.sub("COEFFS_LINEAR"):
            method.coefficients = [
                v for v in (_to_float(t) for t in linear.scalars) if v is not None
            ]
            method.category = "LINEAR"
            return
        if coeffs := block.sub("COEFFS"):
            method.coefficients = [
                v for v in (_to_float(t) for t in coeffs.scalars) if v is not None
            ]
            return
        if tab := block.sub("COMPU_TAB"):
            # 形如：VALUE 0 0.0 1 10.0 2 20.0 ...
            numbers = [v for v in (_to_float(t) for t in tab.scalars) if v is not None]
            pairs = numbers[1:] if numbers and numbers[0] in (0.0, 1.0) else numbers
            method.raw_points = pairs[0::2]
            method.phys_points = pairs[1::2]
            if method.category not in ("TAB_VERB",):
                method.category = "TAB_INTP"

    # -- CHARACTERISTIC ----------------------------------------------------

    def _fill_characteristics(
        self, root: Block, compus: dict[str, CompuMethod], model: CalibrationModel
    ) -> None:
        for block in root.find_all("CHARACTERISTIC"):
            scalars = block.scalars
            if len(scalars) < 9:
                model.warnings.append(
                    f"CHARACTERISTIC 字段不足，已跳过：{scalars[0] if scalars else '?'}"
                )
                continue

            name = unquote(scalars[0])
            type_token = scalars[2].upper()
            char_type = _CHAR_TYPE_BY_TOKEN.get(type_token, CharacteristicType.VALUE)
            address = _to_int(scalars[3])
            record_layout = unquote(scalars[4])
            compu_name = unquote(scalars[6])

            char = Characteristic(
                path=SymbolPath.parse(name),
                name=name,
                type=char_type,
                data_type=_guess_data_type(record_layout, char_type),
                compu=compus.get(compu_name, CompuMethod(name=compu_name)),
                address=address,
                limits=Limits(
                    lower=_to_float(scalars[7]),
                    upper=_to_float(scalars[8]),
                ),
                description=unquote(scalars[1]),
            )

            if ext := block.sub("EXTENDED_LIMITS"):
                ext_values = [_to_float(t) for t in ext.scalars]
                char.extended_limits = Limits(
                    lower=ext_values[0] if ext_values else None,
                    upper=ext_values[1] if len(ext_values) > 1 else None,
                )
            if number := block.sub("NUMBER"):
                char.array_size = _to_int(number.scalars[0] if number.scalars else None)
                if char.array_size:
                    char.dims = [char.array_size]
            if fmt := block.sub("FORMAT"):
                char.compu.format = unquote(fmt.scalars[0]) if fmt.scalars else ""
            if block.sub("READ_ONLY") is not None:
                char.read_only = True

            # 轴下标由 A2L 的 MATRIX_DIM / AXIS_DESCR 数量决定
            axis_descr = block.subs("AXIS_DESCR")
            if axis_descr:
                char.dims = [1] * len(axis_descr)
            if matrix := block.sub("MATRIX_DIM"):
                dims = [_to_int(t) for t in matrix.scalars]
                char.dims = [d for d in dims if d]

            # SYMBOL_LINK 提供符号名（比地址稳定）
            if link := block.sub("SYMBOL_LINK"):
                symbol = unquote(link.scalars[0]) if link.scalars else ""
                if symbol:
                    char.name = symbol
                    char.path = SymbolPath.parse(symbol)

            model.characteristics[char.path.text] = char

    # -- AXIS_DESCR --------------------------------------------------------

    def _fill_axes(
        self, root: Block, compus: dict[str, CompuMethod], model: CalibrationModel
    ) -> None:
        for char_block in root.find_all("CHARACTERISTIC"):
            scalars = char_block.scalars
            if not scalars:
                continue
            owner = unquote(scalars[0])
            for index, descr in enumerate(char_block.subs("AXIS_DESCR")):
                values = descr.scalars
                if len(values) < 4:
                    continue
                kind = _AXIS_KIND_BY_TOKEN.get(values[0].upper(), AxisKind.STD_AXIS)
                compu_name = unquote(values[2]) if len(values) > 2 else ""
                count = _to_int(values[3]) or 0

                axis_name = f"{owner}_axis{index}"
                axis = Axis(
                    path=SymbolPath.parse(axis_name),
                    kind=kind,
                    count=count,
                    compu=compus.get(compu_name, CompuMethod(name=compu_name)),
                    input_quantity=unquote(values[1]) if len(values) > 1 else "",
                    address=_to_int(values[6]) if len(values) > 6 else None,
                )
                if fixed := descr.sub("FIX_AXIS_PAR"):
                    numbers = [_to_float(t) for t in fixed.scalars]
                    start = numbers[0] if numbers and numbers[0] is not None else 0.0
                    step = numbers[1] if len(numbers) > 1 and numbers[1] else 1.0
                    axis.fixed_points = [start + i * step for i in range(count)]

                model.axes[axis.path.text] = axis

                if owner in model.characteristics:
                    model.characteristics[owner].axes.append(axis)

    # -- GROUP -------------------------------------------------------------

    def _fill_groups(self, root: Block, model: CalibrationModel) -> None:
        for block in root.find_all("GROUP"):
            scalars = block.scalars
            if not scalars:
                continue
            group_name = unquote(scalars[0])
            model.groups.append(group_name)
            for ref in block.find_all("REF_CHARACTERISTIC"):
                for token in ref.scalars:
                    name = unquote(token)
                    char = model.characteristics.get(name)
                    if char is not None and not char.group:
                        char.group = group_name

    # -- 元信息 ------------------------------------------------------------

    def _fill_metadata(self, root: Block, model: CalibrationModel) -> None:
        meta: dict[str, Any] = {}
        if project := root.sub("PROJECT"):
            scalars = project.scalars
            if scalars:
                meta["project"] = unquote(scalars[0])
            if len(scalars) > 1:
                meta["project_comment"] = unquote(scalars[1])
        modules = [unquote(b.scalars[0]) for b in root.find_all("MODULE") if b.scalars]
        meta["modules"] = modules
        for section in ("MEASUREMENT", "RECORD_LAYOUT", "MOD_PAR", "MOD_COMMON"):
            meta[f"{section.lower()}_count"] = len(root.find_all(section))
        model.metadata = meta  # type: ignore[attr-defined]
