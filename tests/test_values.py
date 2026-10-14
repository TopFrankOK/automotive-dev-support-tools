"""值集加载与绑定。

这些用例在守一条容易被人忽略、但出事就是数据事故的线：
**值集与描述文件对不上的部分必须被报告出来，不能静默丢弃。**

「贴完值了但某个量其实还是旧的」不会报错，只会让某次试验的结论
悄悄错掉 —— 这是标定工程里最难查的一类问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from automotive_dev_support.core.models.calibration import (
    CalibrationModel,
    ChangeKind,
    Characteristic,
    CharacteristicType,
    CompuMethod,
    DataType,
    ValueEntry,
    ValueSet,
    bind_values,
)
from automotive_dev_support.core.models.common import SymbolPath
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, JobContext
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.diff_calibration import compare_models
from automotive_dev_support.plugins.parser_a2l import A2lParser
from automotive_dev_support.plugins.parser_cdfx import CdfxParser

from conftest import PLUGINS_DIR, SAMPLE_DATA

CTX = JobContext(job_id="test")


def _parse_cdfx(name: str) -> ValueSet:
    out = CdfxParser().parse(FileSource(SAMPLE_DATA / name), CTX)
    assert not out.warnings, f"样本 {name} 不应有解析告警：{out.warnings}"
    return out.document


def _parse_a2l(name: str) -> CalibrationModel:
    return A2lParser().parse(FileSource(SAMPLE_DATA / name), CTX).document


@pytest.fixture
def v1() -> ValueSet:
    return _parse_cdfx("sample_v1.cdfx")


@pytest.fixture
def v2() -> ValueSet:
    return _parse_cdfx("sample_v2.cdfx")


@pytest.fixture
def a2l_v1() -> CalibrationModel:
    return _parse_a2l("sample_v1.a2l")


@pytest.fixture
def a2l_v2() -> CalibrationModel:
    return _parse_a2l("sample_v2.a2l")


# -- 解析 --------------------------------------------------------------


def test_cdfx_plugin_is_discoverable():
    """注册表能找到它 —— 证明「新格式 = 新文件夹」这条路是通的。"""
    registry = PluginRegistry()
    registry.load_all([PLUGINS_DIR])
    ids = {p.manifest.id for p in registry}
    assert "parser-cdfx" in ids
    assert registry.parsers_for(ArtifactKind.CDFX)


def test_nested_instance_path_is_joined(v1):
    """嵌套实例的符号路径必须沿祖先链拼出来，否则只剩最内层那一段。"""
    assert "EngCal.Kp_Speed" in v1.entries
    assert "Kp_Speed" not in v1.entries
    assert v1.entries["EngCal.Kp_Speed"].physical == [850.0]


def test_flat_instance_matches_directly(v1):
    assert v1.entries["Ki_Speed"].physical == [120.0]


def test_container_has_no_values_of_its_own(v1):
    """容器（STRUCTURE）自身没有值，不能把子实例的值算成它的。"""
    assert "EngCal" not in v1.entries


def test_axis_values_are_kept_separate(v1):
    assert len(v1.axis_values["CoolantTemp"]) == 8
    assert v1.axis_values["CoolantTemp"][0] == -40.0


def test_unit_is_captured(v1):
    assert v1.entries["Temp_Offset"].unit == "degC"


def test_malformed_xml_is_reported_not_raised(tmp_path: Path):
    """坏文件必须给出可读的原因，而不是把整个加载流程炸掉。"""
    bad = tmp_path / "broken.cdfx"
    bad.write_text("<MSRSW><SW-SYSTEMS>oops", encoding="utf-8")
    out = CdfxParser().parse(FileSource(bad), CTX)
    assert out.warnings
    assert out.document.entries == {}


# -- 绑定 --------------------------------------------------------------


def test_exact_match_is_preferred(a2l_v1, v1):
    bound, binding = bind_values(a2l_v1, v1)
    assert "Ki_Speed" in binding.matched
    assert bound.characteristics["Ki_Speed"].raw_values == [120.0]


def test_tail_fallback_is_used_and_reported(a2l_v1, v1):
    """A2L 扁平命名、CDFX 带层级 —— 末段回退能救急，但必须说出来。"""
    bound, binding = bind_values(a2l_v1, v1)
    assert "Kp_Speed" in binding.matched_by_fallback
    assert bound.characteristics["Kp_Speed"].raw_values == [850.0]
    assert any("回退匹配" in w for w in binding.warnings)


def test_missing_in_model_is_reported(a2l_v1, v1):
    _, binding = bind_values(a2l_v1, v1)
    assert binding.missing_in_model == ["Obsolete_Gain"]
    assert not binding.is_clean


def test_missing_in_values_is_reported(a2l_v1, v1):
    bound, binding = bind_values(a2l_v1, v1)
    assert "Torque_Map" in binding.missing_in_values
    # 没拿到值的量必须保持「未加载」，不能被当成 0 参与比对
    assert bound.characteristics["Torque_Map"].raw_values is None


def test_physical_is_converted_to_raw_by_compu(a2l_v1, v1):
    """Temp_Offset 是 LINEAR(0.1, -40)：物理 -5 → 原始 350。

    物理值 = 0.1 × 原始值 − 40，反过来 原始值 = (物理值 + 40) / 0.1。
    """
    bound, _ = bind_values(a2l_v1, v1)
    assert bound.characteristics["Temp_Offset"].raw_values == [350.0]


def test_integer_type_is_quantized(a2l_v1, v1):
    """整数类型存不了小数，物理值反算后必须量化。"""
    bound, _ = bind_values(a2l_v1, v1)
    raw = bound.characteristics["Kp_Speed"].raw_values
    assert raw == [850.0]
    assert all(float(v).is_integer() for v in raw)


def test_axis_values_bind_by_input_quantity(a2l_v1, v1):
    """轴靠输入量名匹配 —— 轴在 A2L 里没有自己的符号名。"""
    bound, _ = bind_values(a2l_v1, v1)
    assert len(bound.axes["Torque_Map_axis1"].fixed_points) == 8


def test_binding_does_not_mutate_input(a2l_v1, v1):
    """同一份描述要能挂多组值，绑定绝不能改输入。"""
    bind_values(a2l_v1, v1)
    assert a2l_v1.characteristics["Kp_Speed"].raw_values is None


def test_binding_is_idempotent(a2l_v1, v1):
    first, b1 = bind_values(a2l_v1, v1)
    second, b2 = bind_values(a2l_v1, v1)
    assert first.characteristics["Kp_Speed"].raw_values == (
        second.characteristics["Kp_Speed"].raw_values
    )
    assert b1.coverage == b2.coverage


def test_dimension_mismatch_skips_the_write():
    """元素个数对不上就别写 —— 半截数据比没有数据危险得多。"""
    curve = Characteristic(
        path=SymbolPath.parse("Test_Curve"),
        type=CharacteristicType.CURVE,
        data_type=DataType.UINT16,
        dims=[3],
    )
    model = CalibrationModel()
    model.characteristics[curve.path.text] = curve

    valueset = ValueSet()
    valueset.entries["Test_Curve"] = ValueEntry(
        path=SymbolPath.parse("Test_Curve"), physical=[1.0, 2.0, 3.0, 4.0, 5.0]
    )

    bound, binding = bind_values(model, valueset)
    assert bound.characteristics["Test_Curve"].raw_values is None
    assert binding.dimension_mismatch[0]["expected"] == 3
    assert binding.dimension_mismatch[0]["actual"] == 5


def test_coverage_reflects_how_much_got_values(a2l_v1, v1):
    _, binding = bind_values(a2l_v1, v1)
    # 5 个标定量里 4 个拿到值
    assert binding.coverage == pytest.approx(4 / 5)


# -- 端到端：加载值之后，比对才看得到数值改动 --------------------------


def test_end_to_end_value_changes_are_visible(a2l_v1, a2l_v2, v1, v2):
    """这是 P1 的全部意义：不加载值集，数值改动根本看不见。"""
    base, _ = bind_values(a2l_v1, v1)
    cand, _ = bind_values(a2l_v2, v2)

    changeset = compare_models(base, cand, baseline_label="v1", candidate_label="v2")
    by_path = {c.path: c for c in changeset.changed}

    kp = by_path["Kp_Speed"]
    assert ChangeKind.VALUES_CHANGED in kp.kinds
    change = kp.value_changes[0]
    assert change.phys_before == 850.0
    assert change.phys_after == 920.0
    assert change.delta_physical == 70.0


def test_values_unchanged_but_attrs_changed_is_not_a_value_change(
    a2l_v1, a2l_v2, v1, v2
):
    """Idle_Base 的值没变，只是转换方法换了 —— 不能报成数值改动。"""
    base, _ = bind_values(a2l_v1, v1)
    cand, _ = bind_values(a2l_v2, v2)

    by_path = {c.path: c for c in compare_models(base, cand).changed}
    idle = by_path["Idle_Base"]
    assert ChangeKind.ATTRS_CHANGED in idle.kinds
    assert ChangeKind.VALUES_CHANGED not in idle.kinds
    assert idle.value_changes == []


def test_without_values_no_value_change_is_reported(a2l_v1, a2l_v2):
    """对照组：不加载值集时，Kp_Speed 只有属性/地址层面的差异。"""
    by_path = {c.path: c for c in compare_models(a2l_v1, a2l_v2).changed}
    assert ChangeKind.VALUES_CHANGED not in by_path["Kp_Speed"].kinds


def test_added_and_removed_still_work_with_values(a2l_v1, a2l_v2, v1, v2):
    base, _ = bind_values(a2l_v1, v1)
    cand, _ = bind_values(a2l_v2, v2)
    changeset = compare_models(base, cand)

    assert [c.path for c in changeset.removed] == ["Temp_Offset"]
    assert [c.path for c in changeset.added] == ["Fuel_Cut_Threshold"]
    # 新增的量拿到了值
    assert cand.characteristics["Fuel_Cut_Threshold"].raw_values == [2400.0]


def test_summary_counts_value_changes(a2l_v1, a2l_v2, v1, v2):
    base, _ = bind_values(a2l_v1, v1)
    cand, _ = bind_values(a2l_v2, v2)
    summary = compare_models(base, cand).summary()
    assert summary["value_changes"] >= 1
    assert summary["meaningful"] >= 3


def test_compu_change_keeps_physical_comparable():
    """改了转换方法但物理值没变 → 物理值层面不该报差异。

    这才是「比物理值不比原始值」的价值：ECU 里的整数整片变了，
    但工程师看到的值一个都没动。
    """
    old = Characteristic(
        path=SymbolPath.parse("Gain"),
        data_type=DataType.UINT16,
        compu=CompuMethod(name="CM_Old", category="LINEAR", coefficients=[1.0, 0.0]),
        raw_values=[100.0],
    )
    new = Characteristic(
        path=SymbolPath.parse("Gain"),
        data_type=DataType.UINT16,
        compu=CompuMethod(name="CM_New", category="LINEAR", coefficients=[2.0, 0.0]),
        raw_values=[50.0],
    )
    base = CalibrationModel()
    base.characteristics["Gain"] = old
    cand = CalibrationModel()
    cand.characteristics["Gain"] = new

    by_path = {c.path: c for c in compare_models(base, cand).changed}
    kinds = by_path["Gain"].kinds
    assert ChangeKind.VALUES_CHANGED not in kinds
    assert ChangeKind.ATTRS_CHANGED in kinds
