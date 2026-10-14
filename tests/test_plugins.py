"""插件机制的端到端测试：注册 → 解析 → 比对。

这条链路串起三件必须成立的事：
  1. 注册表能发现并加载插件（清单正确、入口可解析）
  2. parser-a2l 能从真实 A2L 文本产出 CalibrationModel
  3. diff-calibration 能产出 ChangeSet，并且**把地址漂移单独归类**

第 3 条尤其关键。如果地址漂移被算成「改动」，
报告里 90% 的条目都是噪音，这个功能就白做了。
"""

from __future__ import annotations

import pytest

from conftest import PLUGINS_DIR, SAMPLE_DATA
from automotive_dev_support.core.models.calibration import (
    CalibrationModel,
    ChangeKind,
    ChangeSet,
    CharacteristicType,
    DataType,
)
from automotive_dev_support.core.ports import AnalysisInput, PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, JobContext
from automotive_dev_support.core.skeleton.capability import PluginCapability
from automotive_dev_support.platform.store import FileSource


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load_all([PLUGINS_DIR])
    return reg


@pytest.fixture(scope="module")
def parser(registry):
    plugins = registry.parsers_for(ArtifactKind.A2L)
    assert plugins, "没有找到能处理 A2L 的解析器插件"
    return plugins[0].instance


@pytest.fixture(scope="module")
def differ(registry):
    plugins = registry.by_capability(PluginCapability.ANALYZER)
    assert plugins, "没有找到分析器插件"
    return plugins[0].instance


def _parse(parser, filename: str):
    source = FileSource(SAMPLE_DATA / filename)
    return parser.parse(source, JobContext(job_id="test"))


@pytest.fixture(scope="module")
def v1(parser):
    return _parse(parser, "sample_v1.a2l")


@pytest.fixture(scope="module")
def v2(parser):
    return _parse(parser, "sample_v2.a2l")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_registry_summary(registry):
    summary = registry.summary()
    assert summary["total"] >= 2
    assert summary["broken"] == [], f"有插件加载失败：{summary['broken']}"
    assert "parser-a2l" in summary["capabilities"]["parser"]
    assert "diff-calibration" in summary["capabilities"]["analyzer"]


def test_registry_isolates_broken_plugin(tmp_path):
    """一个坏插件不能影响其它插件，更不能影响启动。

    这是注册表存在的首要理由：插件是外挂的，
    它坏了只应该是「这个功能不可用」，而不是「软件打不开」。
    """
    bad = tmp_path / "broken-plugin"
    bad.mkdir()
    (bad / "manifest.toml").write_text(
        '[plugin]\nid = "broken"\ncapabilities = ["parser"]\nentry = "不存在的模块:x"\n',
        encoding="utf-8",
    )
    reg = PluginRegistry()
    reg.load_all([tmp_path])

    assert len(reg) == 1
    assert not reg.get("broken").usable
    assert "导入失败" in reg.get("broken").error
    assert reg.usable() == []


def test_registry_rejects_wrong_api_version(tmp_path):
    plugin_dir = tmp_path / "future-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.toml").write_text(
        "[plugin]\n"
        'id = "future"\n'
        'api_version = "99"\n'
        'capabilities = ["parser"]\n'
        'entry = "automotive_dev_support.plugins.parser_a2l.parser:A2lParser"\n',
        encoding="utf-8",
    )
    reg = PluginRegistry()
    reg.load_all([tmp_path])
    assert "契约版本不匹配" in reg.get("future").error


# ---------------------------------------------------------------------------
# A2L 解析
# ---------------------------------------------------------------------------


def test_parse_returns_artifact_and_model(v1):
    assert v1.artifact.kind is ArtifactKind.A2L
    assert v1.artifact.producer == "parser-a2l"
    assert isinstance(v1.document, CalibrationModel)


def test_characteristics_indexed_by_symbol_path(v1):
    """索引键必须是符号路径。

    这是整套跨版本比对能成立的前提 —— 按地址索引会让每次比对都是噪音。
    """
    assert set(v1.document.characteristics) == {
        "Kp_Speed",
        "Ki_Speed",
        "Temp_Offset",
        "Idle_Base",
        "Torque_Map",
    }
    assert v1.document.get("Kp_Speed").address == 0x80001234


def test_compu_method_resolved(v1):
    temp = v1.document.get("Temp_Offset")
    assert temp.compu.name == "CM_TempLinear"
    assert temp.compu.category == "LINEAR"
    assert temp.compu.coefficients == [0.1, -40.0]
    assert temp.compu.unit == "degC"
    # 物理值换算：原始 1000 → 0.1*1000 - 40 = 60.0
    assert temp.compu.to_physical(1000) == pytest.approx(60.0)
    # 反向换算应能回到原值
    assert temp.compu.to_raw(60.0) == pytest.approx(1000.0)


def test_data_type_inferred_from_record_layout(v1):
    assert v1.document.get("Kp_Speed").data_type is DataType.UINT16
    assert v1.document.get("Temp_Offset").data_type is DataType.SINT16


def test_map_dimensions_and_axes(v1):
    torque = v1.document.get("Torque_Map")
    assert torque.type is CharacteristicType.MAP
    assert torque.dims == [16, 8, 1]
    assert torque.element_count == 128
    assert len(torque.axes) == 2
    assert torque.axes[0].count == 16
    assert torque.axes[1].count == 8


def test_extended_limits_parsed(v1):
    temp = v1.document.get("Temp_Offset")
    assert temp.limits.lower == -40
    assert temp.limits.upper == 200
    assert temp.extended_limits.lower == -50
    assert temp.extended_limits.upper == 250


def test_groups_assigned(v1):
    assert v1.document.get("Torque_Map").group == "TorqueGroup"
    assert v1.document.get("Kp_Speed").group == "TorqueGroup"
    assert v1.document.get("Idle_Base").group == "IdleGroup"
    assert "TorqueGroup" in v1.document.groups


def test_metadata_collected(v1):
    meta = v1.document.metadata
    assert meta["project"] == "DemoProject"
    assert meta["modules"] == ["EngineModule"]
    assert meta["measurement_count"] == 1


def test_by_prefix_filter(v1):
    assert len(v1.document.by_prefix("Torque")) == 1
    assert len(v1.document.with_values()) == 0  # A2L 只有描述，没有值


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def changeset(differ, v1, v2) -> ChangeSet:
    result = differ.analyze(
        [
            AnalysisInput(artifact=v1.artifact, document=v1.document),
            AnalysisInput(artifact=v2.artifact, document=v2.document),
        ],
        {},
        JobContext(job_id="diff"),
    )
    assert isinstance(result.payload, ChangeSet)
    return result.payload


def test_added_and_removed(changeset):
    assert [c.path for c in changeset.added] == ["Fuel_Cut_Threshold"]
    assert [c.path for c in changeset.removed] == ["Temp_Offset"]


def test_address_drift_is_its_own_category(changeset):
    """本测试是整套比对逻辑的核心断言。

    Kp_Speed 在两个版本里语义完全一致，只是地址从 0x80001234
    漂到 0x80001244（App 重编一次就会发生）。它必须被识别为
    address_moved，而不是「改动」。
    """
    moved = changeset.address_moves()
    assert [c.path for c in moved] == ["Kp_Speed"]
    assert moved[0].kinds == [ChangeKind.ADDRESS_MOVED]
    assert moved[0].address_before == 0x80001234
    assert moved[0].address_after == 0x80001244
    assert not moved[0].is_meaningful


def test_attribute_changes_detected(changeset):
    by_path = {c.path: c for c in changeset.changed}

    ki = by_path["Ki_Speed"]
    assert ChangeKind.ATTRS_CHANGED in ki.kinds
    fields = {f.field for f in ki.attribute_changes}
    assert "upper" in fields

    # Idle_Base 从 CM_Pct 换到 CM_None，两个字段都会变：
    # 转换方法名变了，转换方法自带的显示单位也从 "%" 变成空。
    # 两者都如实报告 —— 「哪些量的单位变了」是标定工程师的真实筛选需求，
    # 把它并进 compu 会让这条信息在报告里消失。
    idle = by_path["Idle_Base"]
    assert ChangeKind.ATTRS_CHANGED in idle.kinds
    assert {f.field for f in idle.attribute_changes} == {"compu", "unit"}
    # 转换类别没变（CM_Pct 与 CM_None 都是 IDENTITY），不该报出来
    assert "compu_category" not in {f.field for f in idle.attribute_changes}


def test_unchanged_characteristic_not_reported(changeset):
    """Torque_Map 两个版本完全一致，不该出现在报告里。

    看起来是废话，但「比对结果里全是没变的东西」是这个功能
    最常见的失败方式 —— 通常源于按地址而非按符号路径对齐。
    """
    assert "Torque_Map" not in {c.path for c in changeset.changed}


def test_summary_separates_noise_from_signal(changeset):
    summary = changeset.summary()
    assert summary["added"] == 1
    assert summary["removed"] == 1
    assert summary["changed"] == 3
    assert summary["address_moved"] == 1
    # meaningful 排除了纯地址漂移，是报告默认展示的口径
    assert summary["meaningful"] == 4
    assert len(changeset.meaningful_changes()) == 4


def test_value_changes_when_values_loaded(differ, parser):
    """A2L 只给描述，值来自值集文件。

    这里手工塞入原始值，模拟「值集已加载」之后再做一次比对 ——
    验证数值比对分支本身是通的。
    """
    m1 = _parse(parser, "sample_v1.a2l").document
    m2 = _parse(parser, "sample_v2.a2l").document

    m1.characteristics["Kp_Speed"].raw_values = [100, 200, 300]
    m2.characteristics["Kp_Speed"].raw_values = [100, 250, 300]
    m1.characteristics["Idle_Base"].raw_values = [50]
    m2.characteristics["Idle_Base"].raw_values = [80]

    result = differ.analyze(
        [
            AnalysisInput(artifact=_fake_artifact("v1"), document=m1),
            AnalysisInput(artifact=_fake_artifact("v2"), document=m2),
        ],
        {},
        JobContext(job_id="diff"),
    )
    cs: ChangeSet = result.payload
    by_path = {c.path: c for c in cs.changed}

    kp = by_path["Kp_Speed"]
    assert ChangeKind.VALUES_CHANGED in kp.kinds
    assert kp.value_count == 1
    assert kp.value_changes[0].index == 1
    assert kp.value_changes[0].raw_before == 200
    assert kp.value_changes[0].raw_after == 250

    idle = by_path["Idle_Base"]
    assert ChangeKind.VALUES_CHANGED in idle.kinds
    assert idle.value_changes[0].delta_physical == pytest.approx(30.0)


def test_same_values_produce_no_change(differ, parser):
    """值完全相同时不应产生任何条目。

    浮点比较用严格相等而不是 epsilon —— 标定数据是工程事实，
    「差 1e-9」在标定语境内就是「变了」。
    """
    m1 = _parse(parser, "sample_v1.a2l").document
    m2 = _parse(parser, "sample_v2.a2l").document
    for name in ("Kp_Speed", "Idle_Base"):
        m1.characteristics[name].raw_values = [1, 2, 3]
        m2.characteristics[name].raw_values = [1, 2, 3]

    result = differ.analyze(
        [
            AnalysisInput(artifact=_fake_artifact("v1"), document=m1),
            AnalysisInput(artifact=_fake_artifact("v2"), document=m2),
        ],
        {},
        JobContext(job_id="diff"),
    )
    cs: ChangeSet = result.payload
    assert all(ChangeKind.VALUES_CHANGED not in c.kinds for c in cs.changed)


def _fake_artifact(label: str):
    from automotive_dev_support.core.skeleton import Artifact, ArtifactRef

    return Artifact(ref=ArtifactRef(uri=f"memory://{label}"), label=label)


def test_missing_input_yields_warning_not_crash(differ):
    """输入不足时应当给出可读的提示，而不是抛异常。

    解析失败、只选了一个文件、选错了文件 —— 这些都会发生。
    让它们表现为异常会让界面必须遍到处 try/except。
    """
    result = differ.analyze([], {}, JobContext(job_id="empty"))
    assert result.payload is None
    assert result.warnings
