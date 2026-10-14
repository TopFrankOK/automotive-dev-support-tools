"""授权服务的端到端测试（含持久化）。

与 `test_licensing.py` 的分工：
    那边测**判定逻辑**（五道关卡），纯函数，快。
    这边测**接线是否正确**（指纹 → 存储 → 校验 → 落盘 → 重启后仍有效）。

后者容易被忽略但同样致命：判定全对，但许可码没落盘、
或者状态记录每次重启都清零，用户的实际体验依然是坏的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CODE_A, MACHINE_A, MACHINE_B
from automotive_dev_support.core.licensing import (
    Expired,
    FeatureNotLicensed,
    LicenseError,
    MachineMismatch,
    NotActivated,
    generate_keypair,
    issue,
    private_key_from_hex,
)
from automotive_dev_support.platform.licensing.service import LicensingService
from automotive_dev_support.platform.licensing.store import FileStore


@pytest.fixture
def keypair():
    priv_hex, pub_b64 = generate_keypair()
    return private_key_from_hex(priv_hex), pub_b64


@pytest.fixture
def service(tmp_path: Path, monkeypatch, keypair):
    """构造一个完全不碰真实硬件与本机注册表的授权服务。

    测试必须无副作用：读注册表、读主板 UUID 都会让测试变慢、
    不可重放、并在 CI 上直接失败。
    """
    _, pub_b64 = keypair
    monkeypatch.setattr(
        LicensingService, "machine_code", staticmethod(lambda: CODE_A)
    )
    monkeypatch.setattr(
        LicensingService, "fingerprint", staticmethod(lambda: MACHINE_A)
    )
    return LicensingService(
        pub_b64,
        data_dir=tmp_path,
        stores=[FileStore(tmp_path / "state.json", b"test-salt")],
    )


def make_code(
    keypair,
    *,
    days: int = 30,
    grace: int = 7,
    machine: str = CODE_A,
    fingerprint: dict[str, str] | None = None,
    features=None,
) -> str:
    private, _ = keypair
    code, _ = issue(
        private,
        lic_id="L-2026-0001",
        licensee="张三",
        machine_code=machine,
        fingerprint=MACHINE_A if fingerprint is None else fingerprint,
        days=days,
        grace_days=grace,
        features=features or [],
    )
    return code


def make_foreign_code(keypair, **kw) -> str:
    """签给另一台机器的码（指纹与本机不符）。"""
    return make_code(
        keypair, machine="0000-0000-0000-0000", fingerprint=MACHINE_B, **kw
    )


# ---------------------------------------------------------------------------
# 激活流程
# ---------------------------------------------------------------------------


def test_status_before_activation(service):
    status = service.status()
    assert status.state == "not_activated"
    assert status.machine_code == CODE_A
    assert not status.usable
    # 未激活时也要把机器码显示出来，用户才知道该发什么给作者
    assert CODE_A in status.to_display()


def test_activate_success(service, keypair):
    status = service.activate(make_code(keypair))
    assert status.state == "valid"
    assert status.licensee == "张三"
    assert status.lic_id == "L-2026-0001"
    assert status.to_display().startswith("有效")


def test_activation_persists_across_restart(service, keypair, tmp_path):
    """激活必须落盘 —— 否则用户每开一次软件就要重新粘一次许可码。"""
    service.activate(make_code(keypair))

    reopened = LicensingService(
        keypair[1],
        data_dir=tmp_path,
        stores=[FileStore(tmp_path / "state.json", b"test-salt")],
    )
    assert reopened.status().state == "valid"


def test_activation_accepts_mangled_clipboard(service, keypair):
    """微信转发会带换行和空格。这一步不能失败。"""
    code = make_code(keypair)
    mangled = "\n".join(code[i : i + 30] for i in range(0, len(code), 30))
    assert service.activate(mangled).state == "valid"


def test_invalid_code_not_persisted(service, keypair):
    """验证不通过就不能落盘。

    否则这张坏码会一直留在磁盘上，之后每次启动都失败，
    而用户根本不知道是自己哪一步弄坏的。
    """
    with pytest.raises(MachineMismatch):
        service.activate(make_foreign_code(keypair))
    assert service.current_code() is None
    assert service.status().state == "not_activated"


def test_activate_code_for_other_machine(service, keypair):
    with pytest.raises(MachineMismatch):
        service.activate(make_foreign_code(keypair))
    assert service.status().state == "not_activated"


def test_deactivate_keeps_state_record(service, keypair):
    """注销只删许可码，**不重置时长记录**。

    否则用户只要注销再重新激活，就能把累计运行时长清零 ——
    预算这道防线会直接失效。
    """
    service.activate(make_code(keypair))
    service.clock.add_hours(42.0)
    service.deactivate()
    assert service.status().state == "not_activated"
    assert service.clock.load().used_hours == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# 到期与宽限
# ---------------------------------------------------------------------------


def test_expired_shows_expired_state(service, keypair):
    code = make_code(keypair, days=-10, grace=0)
    with pytest.raises(Expired):
        service.activate(code)
    # 激活失败 → 未激活。用直接写文件的方式模拟「过期后一直放着」
    _force_write_code(service, code)
    status = service.status()
    assert status.state == "expired"
    assert status.licensee == "张三"  # 仍能读出是谁的码，便于对账
    assert status.hint


def test_grace_period_status_and_read_only(service, keypair):
    code = make_code(keypair, days=-1, grace=7)
    _force_write_code(service, code)

    status = service.status()
    assert status.state == "grace"
    assert status.read_only
    assert status.usable  # 还能用，只是降级

    # 宽限期内允许读
    assert service.require() is not None
    # 但拒绝任何写入路径
    with pytest.raises(FeatureNotLicensed):
        service.require_mutable()


# ---------------------------------------------------------------------------
# 与业务层的接口
# ---------------------------------------------------------------------------


def test_require_without_activation(service):
    with pytest.raises(NotActivated):
        service.require()


def test_require_feature_gate(service, keypair):
    code = make_code(keypair, features=["calibration-diff"])
    service.activate(code)
    assert service.require("calibration-diff") is not None
    with pytest.raises(FeatureNotLicensed):
        service.require("flashing")


def test_granted_features(service, keypair):
    assert service.granted_features() == []
    service.activate(make_code(keypair, features=["a", "b"]))
    assert set(service.granted_features()) == {"a", "b"}


def test_status_never_raises_on_corrupt_file(service):
    """界面会在任意时刻调用 status()，它绝不能抛异常。

    磁盘文件被手工改坏、写了一半断电 —— 这些都会发生。
    """
    path = service._license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("WF1.这不是一个合法的许可码.xxx", encoding="utf-8")
    status = service.status()
    assert status.state in ("invalid", "not_activated")
    assert status.message or status.hint


def test_error_carries_actionable_hint(service, keypair):
    """失败提示必须能指导下一步动作，而不是一句「授权失败」。"""
    with pytest.raises(LicenseError) as excinfo:
        service.activate(make_foreign_code(keypair))
    assert excinfo.value.hint
    assert "机器码" in excinfo.value.hint


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _force_write_code(service: LicensingService, code: str) -> None:
    """绕过 activate 的校验直接落盘，用于构造「已过期」这类状态。

    真实场景是：用户激活了一张有效码，然后时间流逝到过期。
    直接调 activate 是造不出这个状态的 —— 它绝不会保存一张已过期的码。
    """
    path = service._license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
