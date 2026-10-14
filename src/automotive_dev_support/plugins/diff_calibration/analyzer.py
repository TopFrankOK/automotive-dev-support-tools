"""标定比对插件。

按符号路径对齐两个 `CalibrationModel`，产出 `ChangeSet`。

## 三条决定成败的设计

1. **按符号路径对齐，不按地址。**
   App 重编一次，几百上千个标定量的地址会整体漂移。
   按地址比对的结果是「全都变了」，报告没人看，真正的改动淹没其中。
   地址变化被单独归为 `address_moved`，默认折叠。

2. **比物理值，不比原始值。**
   如果某个标定量的 COMPU_METHOD 系数被改过，原始值整片都会变，
   但物理值可能一个都没动。反过来，改了系数导致物理值全变，
   也应该能被看出来。所以两者都要比，但报告以物理值为准。

3. **数值全等才叫没变。**
   浮点比较用严格相等而不是 epsilon —— 标定数据是工程事实，
   不是测量值。「差 1e-9」在标定语境里就是「变了」，
   尤其是整数类型的标定量。容差由调用方通过 params 显式给出。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from automotive_dev_support.core.models.calibration import (
    CalibrationModel,
    ChangeKind,
    ChangeSet,
    Characteristic,
    CharacteristicChange,
    FieldChange,
    ValueChange,
    describe,
)
from automotive_dev_support.core.ports.plugin import AnalysisInput, AnalysisOutcome
from automotive_dev_support.core.skeleton import JobContext

__all__ = ["CalibrationDiffer", "compare_models"]


class CalibrationDiffer:
    """`core.ports.Analyzer` 的实现。"""

    def analyze(
        self,
        inputs: Sequence[AnalysisInput],
        params: dict[str, Any],
        ctx: JobContext,
    ) -> AnalysisOutcome:
        if len(inputs) < 2:
            return AnalysisOutcome(
                warnings=["需要两份标定数据才能比对（先选基准版，再选对比版）"]
            )

        baseline = _as_model(inputs[0])
        candidate = _as_model(inputs[1])
        if baseline is None or candidate is None:
            return AnalysisOutcome(
                warnings=[
                    "输入中没有可用的标定模型。请先用 parser-a2l 解析 A2L 描述文件。"
                ]
            )

        tolerance = float(params.get("tolerance", 0.0))
        compare_values = bool(params.get("compare_values", True))

        changeset = compare_models(
            baseline,
            candidate,
            baseline_label=inputs[0].artifact.label or inputs[0].artifact.ref.uri,
            candidate_label=inputs[1].artifact.label or inputs[1].artifact.ref.uri,
            tolerance=tolerance,
            compare_values=compare_values,
            progress=ctx.report,
        )

        return AnalysisOutcome(
            payload=changeset,
            findings=_to_findings(changeset),
            warnings=list(changeset.warnings),
        )


def _as_model(source: AnalysisInput) -> CalibrationModel | None:
    doc = source.document
    return doc if isinstance(doc, CalibrationModel) else None


def compare_models(
    baseline: CalibrationModel,
    candidate: CalibrationModel,
    *,
    baseline_label: str = "",
    candidate_label: str = "",
    tolerance: float = 0.0,
    compare_values: bool = True,
    progress: Any = None,
) -> ChangeSet:
    """纯函数形式的比对。便于单测，也便于在插件之外复用。"""
    result = ChangeSet(baseline_label=baseline_label, candidate_label=candidate_label)

    base_keys = set(baseline.characteristics)
    cand_keys = set(candidate.characteristics)
    total = len(base_keys | cand_keys)
    done = 0

    def tick(message: str = "") -> None:
        nonlocal done
        done += 1
        if progress is not None and (done % 200 == 0 or done == total):
            progress(done, total, message)

    for path in sorted(base_keys - cand_keys):
        char = baseline.characteristics[path]
        result.removed.append(
            CharacteristicChange(
                path=path,
                name=char.name,
                kinds=[ChangeKind.REMOVED],
                address_before=char.address,
                group=char.group,
            )
        )
        tick()

    for path in sorted(cand_keys - base_keys):
        char = candidate.characteristics[path]
        result.added.append(
            CharacteristicChange(
                path=path,
                name=char.name,
                kinds=[ChangeKind.ADDED],
                address_after=char.address,
                group=char.group,
            )
        )
        tick()

    for path in sorted(base_keys & cand_keys):
        before = baseline.characteristics[path]
        after = candidate.characteristics[path]
        change = _diff_one(before, after, tolerance, compare_values)
        if change is not None:
            result.changed.append(change)
        tick()

    return result


def _diff_one(
    before: Characteristic,
    after: Characteristic,
    tolerance: float,
    compare_values: bool,
) -> CharacteristicChange | None:
    kinds: list[ChangeKind] = []
    attr_changes: list[FieldChange] = []
    value_changes: list[ValueChange] = []

    flat_before = describe(before)
    flat_after = describe(after)
    for field in flat_before:
        if flat_before[field] != flat_after[field]:
            attr_changes.append(
                FieldChange(field=field, before=flat_before[field], after=flat_after[field])
            )
    if attr_changes:
        kinds.append(ChangeKind.ATTRS_CHANGED)

    if compare_values:
        value_changes = _diff_values(before, after, tolerance)
        if value_changes:
            kinds.append(ChangeKind.VALUES_CHANGED)

    # 地址变化单独归类 —— 它是「编译产物差异」，不是「人的改动」。
    # 只有在没有其它变化时才单独成一条，否则只是附带信息。
    if before.address != after.address:
        if not kinds:
            kinds.append(ChangeKind.ADDRESS_MOVED)

    if not kinds:
        return None

    return CharacteristicChange(
        path=before.path.text,
        name=after.name or before.name,
        kinds=kinds,
        attribute_changes=attr_changes,
        value_changes=value_changes,
        address_before=before.address,
        address_after=after.address,
        group=after.group or before.group,
    )


def _diff_values(
    before: Characteristic, after: Characteristic, tolerance: float
) -> list[ValueChange]:
    """逐元素比对。曲线与图表按扁平下标记录，便于界面定位。"""
    raw_before = before.raw_values
    raw_after = after.raw_values
    if raw_before is None or raw_after is None:
        return []
    if len(raw_before) != len(raw_after):
        # 维度变了，属于结构变更，不在逐元素比对的范畴里
        return []

    phys_before = before.physical_values()
    phys_after = after.physical_values()
    dims = after.dims or before.dims
    cols = dims[1] if len(dims) > 1 else 0

    changes: list[ValueChange] = []
    for index, (rb, ra) in enumerate(zip(raw_before, raw_after)):
        pb = phys_before[index] if index < len(phys_before) else None
        pa = phys_after[index] if index < len(phys_after) else None

        # ★ 以物理值为准，原始值只作记录。
        #
        # 转换方法被改过时，原始值会整片变化，但工程师看到的值
        # 可能一个都没动 —— 那不是数值改动，不该报出来。
        # （转换方法本身的变化由 ATTRS_CHANGED 负责报告。）
        # 反过来的情形必须报：物理值变了，无论原始值变没变。
        phys_changed = pb is None or pa is None or abs(pa - pb) > tolerance
        if not phys_changed:
            continue

        changes.append(
            ValueChange(
                index=index,
                row=(index // cols) if cols else None,
                col=(index % cols) if cols else None,
                raw_before=float(rb),
                raw_after=float(ra),
                phys_before=pb,
                phys_after=pa,
            )
        )
    return changes


def _to_findings(changeset: ChangeSet) -> list[dict[str, Any]]:
    """给界面用的轻量摘要。完整数据在 payload 里。"""
    summary = changeset.summary()
    return [
        {"level": "info", "key": key, "value": value} for key, value in summary.items()
    ]
