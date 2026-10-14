"""授权核心的完整测试。

**这个文件存在的意义比其他测试都大。**
授权的判定逻辑是整个软件里唯一「错了就等于业务失败」的部分：
判宽了，你自己写的工具白送；判严了，同事在试验场打不开软件，
一个电话打到你这里。所以每一道关卡都要有独立用例。

而且因为 `core.licensing` 是纯逻辑（所有外部输入经由 `LocalState`），
这五道关卡可以被完整覆盖，不需要真实硬件、不需要注册表、不需要时间旅行。
—— 这正是当初把它做成纯函数式的理由。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import CODE_A, MACHINE_A, MACHINE_B
from automotive_dev_support.core.licensing import (
    BadSignature,
    BudgetExhausted,
    ClockRollback,
    Expired,
    FeatureNotLicensed,
    LicenseVerifier,
    LocalState,
    MalformedCode,
    MachineMismatch,
    NotYetValid,
    codec,
    generate_keypair,
    issue,
    private_key_from_hex,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def keypair() -> tuple[object, str]:
    priv_hex, pub_b64 = generate_keypair()
    return private_key_from_hex(priv_hex), pub_b64


@pytest.fixture
def verifier(keypair) -> LicenseVerifier:
    _, pub_b64 = keypair
    return LicenseVerifier(pub_b64)


@pytest.fixture
def local_a() -> LocalState:
    return LocalState(machine_code=CODE_A, fingerprint=MACHINE_A)


def make_code(
    keypair,
    *,
    machine: str = CODE_A,
    fingerprint: dict[str, str] | None = None,
    days: int = 30,
    grace_days: int = 7,
    budget_hours: float | None = None,
    features: list[str] | None = None,
    not_before: datetime | None = None,
) -> str:
    """签发一张许可码。

    固定 `now=NOW` 是必要的：如果让签发端读真实墙钟，
    「30 天后到期」这条断言就会随跑测试的时刻漂移 ——
    上午跑是 30 天，下午跑还是 30 天，但基准点已经不是同一个了。
    注入时钟之后，签发的起点与校验的起点是同一个 `NOW`。
    """
    private, _ = keypair
    code, _payload = issue(
        private,
        lic_id="L-2026-0001",
        licensee="张三",
        machine_code=machine,
        fingerprint=MACHINE_A if fingerprint is None else fingerprint,
        days=days,
        grace_days=grace_days,
        budget_hours=budget_hours,
        features=features or [],
        not_before=not_before,
        now=NOW,
    )
    return code


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_valid_code_passes(verifier, keypair, local_a):
    code = make_code(keypair)
    outcome = verifier.verify(code, local_a, now=NOW)
    assert outcome.state == "valid"
    assert outcome.payload.licensee == "张三"
    assert outcome.payload.lic_id == "L-2026-0001"
    assert not outcome.read_only
    assert 29 < outcome.days_left < 31


def test_paste_with_whitespace_still_works(verifier, keypair, local_a):
    """微信转发会掺进换行和空格。

    如果这里严格比对，作者会被「我粘贴了没反应」的求助淹没 ——
    所以 codec.normalize() 必须无条件去掉所有空白。
    """
    code = make_code(keypair)
    mangled = "\n  ".join(code[i : i + 40] for i in range(0, len(code), 40))
    assert "\n" in mangled and "  " in mangled
    outcome = verifier.verify(mangled, local_a, now=NOW)
    assert outcome.state == "valid"


def test_code_is_message_sized(keypair):
    """许可码要能一条微信发出去。超过 800 字符就该重新审视编码方案。"""
    code = make_code(keypair)
    assert len(code) < 800, f"许可码 {len(code)} 字符，太长了"
    assert code.startswith("WF1.")


def test_canonical_bytes_are_deterministic(keypair):
    """签发与校验必须对完全相同的字节做签名。

    字典序不固定是最常见的坑：表现为「明明是我签的却验不过」。
    """
    code = make_code(keypair)
    payload, _, blob = codec.decode(code)
    assert codec.canonical_bytes(payload) == blob


# ---------------------------------------------------------------------------
# 第 1 关：签名
# ---------------------------------------------------------------------------


def test_tampered_payload_rejected(verifier, keypair, local_a):
    """把有效期改长十年 —— 这本来是最诱人的攻击方式。

    签名保护的是**内容**，不是「藏起来的密钥」。
    篡改任何字段都会让签名对不上。
    """
    code = make_code(keypair)
    payload, signature, _ = codec.decode(code)
    forged = payload.model_copy(
        update={"expires_at": payload.expires_at + timedelta(days=3650)}
    )
    with pytest.raises(BadSignature):
        verifier.verify(codec.encode(forged, signature), local_a, now=NOW)


def test_random_garbage_rejected(verifier, local_a):
    with pytest.raises(MalformedCode):
        verifier.verify("这不是许可码", local_a, now=NOW)
    with pytest.raises(MalformedCode):
        verifier.verify("WF1.abc", local_a, now=NOW)


def test_code_from_another_author_rejected(verifier, local_a):
    """别人自己生成一对密钥签出来的码，必须验收失败。"""
    other_priv_hex, _ = generate_keypair()
    other_code, _ = issue(
        private_key_from_hex(other_priv_hex),
        lic_id="L-FAKE",
        licensee="攻击者",
        machine_code=CODE_A,
        fingerprint=MACHINE_A,
        days=30,
    )
    with pytest.raises(BadSignature):
        verifier.verify(other_code, local_a, now=NOW)


# ---------------------------------------------------------------------------
# 第 2 关：机器指纹
# ---------------------------------------------------------------------------


def test_other_machine_rejected(verifier, keypair):
    """拷贝安装包是允许的，拷贝许可码不行。"""
    code = make_code(keypair)
    other = LocalState(machine_code="9999-8888-7777-6666", fingerprint=MACHINE_B)
    with pytest.raises(MachineMismatch):
        verifier.verify(code, other, now=NOW)


def test_fingerprint_tolerance_one_of_two(verifier, keypair):
    """换了一块硬盘不该把人锁死。

    夹具里 MACHINE_A 有三个组件（board / disk / sys）。
    只换硬盘时另外两项仍然命中，达到 fp_threshold=1 就放行 ——
    这是刻意的宽松，换来的是日常几乎不会有人因为硬件变动来找作者。
    """
    code = make_code(keypair)
    changed_disk = dict(MACHINE_A)
    changed_disk["disk"] = "ffffffffffffffff"
    partial = LocalState(machine_code="NEW1-CODE", fingerprint=changed_disk)
    outcome = verifier.verify(code, partial, now=NOW)
    assert outcome.state == "valid"
    assert "disk" not in outcome.matched_components
    assert set(outcome.matched_components) == {"board", "sys"}


def test_all_components_changed_rejected(verifier, keypair):
    code = make_code(keypair)
    other = LocalState(machine_code="9999-8888-7777-6666", fingerprint=MACHINE_B)
    with pytest.raises(MachineMismatch):
        verifier.verify(code, other, now=NOW)


def test_machine_code_only_binding(verifier, keypair):
    """只拿到 16 位机器码时（`fp` 为空）退回机器码比对。

    这是日常最常用的模式：同事只发 16 个字符，作者无需拿到硬件哈希。
    """
    code = make_code(keypair, fingerprint={})
    assert verifier.verify(code, LocalState(machine_code=CODE_A), now=NOW).state == "valid"
    with pytest.raises(MachineMismatch):
        verifier.verify(code, LocalState(machine_code="0000-0000-0000-0000"), now=NOW)


# ---------------------------------------------------------------------------
# 第 3 关：防时钟回拨
# ---------------------------------------------------------------------------


def test_clock_rollback_detected(verifier, keypair):
    """把系统时间调回去，是绕过「一个月有效期」最省事的方式。"""
    code = make_code(keypair)
    state = LocalState(
        machine_code=CODE_A,
        fingerprint=MACHINE_A,
        max_seen=NOW,
    )
    with pytest.raises(ClockRollback):
        verifier.verify(code, state, now=NOW - timedelta(days=40))


def test_clock_drift_within_tolerance_allowed(verifier, keypair):
    """时区调整、夏令时、NTP 校正都会让时间小幅跳动，一律判死会误伤。

    注意这张码必须**早于 NOW 签发**：场景是「许可已经生效、正在使用，
    系统时间随后往回跳了 2 小时」。若把签发时间也设在 NOW，
    这条用例会先撞上第 4 关的 NotYetValid，测不到时钟容差。
    """
    code = make_code(keypair, not_before=NOW - timedelta(days=1))
    state = LocalState(machine_code=CODE_A, fingerprint=MACHINE_A, max_seen=NOW)
    outcome = verifier.verify(code, state, now=NOW - timedelta(hours=2))
    assert outcome.state == "valid"


def test_rollback_gate_runs_before_expiry(verifier, keypair):
    """顺序是刻意的。

    如果先判有效期，被回拨的 `now` 会让「已过期」的许可看起来还有效；
    先识别出时间不可信，才不会用一个被操纵的 now 去下结论。
    """
    code = make_code(keypair, days=1)
    state = LocalState(machine_code=CODE_A, fingerprint=MACHINE_A, max_seen=NOW)
    with pytest.raises(ClockRollback):
        verifier.verify(code, state, now=NOW - timedelta(days=10))


# ---------------------------------------------------------------------------
# 第 4 关：有效期与宽限
# ---------------------------------------------------------------------------


def test_expired_rejected(verifier, keypair, local_a):
    code = make_code(keypair, days=1, grace_days=0)
    with pytest.raises(Expired):
        verifier.verify(code, local_a, now=NOW + timedelta(days=2))


def test_grace_period_degrades_to_read_only(verifier, keypair, local_a):
    """到期后给几天只读，而不是当场锁死。

    车载标定常在试验场或车间，网络差、时间紧。
    直接锁死会让同事当场没法干活 —— 那属于自己给自己找麻烦。
    """
    code = make_code(keypair, days=1, grace_days=7)
    outcome = verifier.verify(code, local_a, now=NOW + timedelta(days=3))
    assert outcome.state == "grace"
    assert outcome.read_only


def test_expired_beyond_grace_rejected(verifier, keypair, local_a):
    code = make_code(keypair, days=1, grace_days=7)
    with pytest.raises(Expired):
        verifier.verify(code, local_a, now=NOW + timedelta(days=20))


def test_not_yet_valid_rejected(verifier, keypair, local_a):
    code = make_code(keypair, not_before=NOW + timedelta(days=3))
    with pytest.raises(NotYetValid):
        verifier.verify(code, local_a, now=NOW)


# ---------------------------------------------------------------------------
# 第 5 关：运行时长预算
# ---------------------------------------------------------------------------


def test_budget_exhausted(verifier, keypair):
    """日历可以被改，但已经消耗掉的运行时长改不小 ——
    这是离线场景下最有效的一道锁。"""
    code = make_code(keypair, budget_hours=100)
    state = LocalState(machine_code=CODE_A, fingerprint=MACHINE_A, used_hours=120)
    with pytest.raises(BudgetExhausted):
        verifier.verify(code, state, now=NOW)


def test_budget_within_limit_ok(verifier, keypair):
    code = make_code(keypair, budget_hours=100)
    state = LocalState(machine_code=CODE_A, fingerprint=MACHINE_A, used_hours=40)
    assert verifier.verify(code, state, now=NOW).state == "valid"


def test_no_budget_means_unlimited(verifier, keypair, local_a):
    code = make_code(keypair)
    state = LocalState(
        machine_code=CODE_A, fingerprint=MACHINE_A, used_hours=100_000.0
    )
    assert verifier.verify(code, state, now=NOW).state == "valid"


# ---------------------------------------------------------------------------
# 功能项
# ---------------------------------------------------------------------------


def test_feature_gate(verifier, keypair, local_a):
    code = make_code(keypair, features=["calibration-diff"])
    assert verifier.verify(code, local_a, feature="calibration-diff", now=NOW)
    with pytest.raises(FeatureNotLicensed):
        verifier.verify(code, local_a, feature="flashing", now=NOW)


def test_empty_features_means_full_license(verifier, keypair, local_a):
    """不列功能 = 全功能授权。

    日常用法就是「只给一个月期限」，逐个列功能名纯属自找麻烦。
    要按功能分级收费时再明确列。
    """
    code = make_code(keypair, features=[])
    assert verifier.verify(code, local_a, feature="flashing", now=NOW)
    assert verifier.verify(code, local_a, feature="anything-at-all", now=NOW)


# ---------------------------------------------------------------------------
# 便捷查询
# ---------------------------------------------------------------------------


def test_payload_helpers(keypair):
    code = make_code(keypair, days=30)
    payload, _, _ = codec.decode(code)
    assert payload.is_active(NOW)
    assert not payload.is_expired(NOW)
    assert not payload.in_grace(NOW)
    after = payload.expires_at + timedelta(days=1)
    assert payload.is_expired(after)
    assert payload.in_grace(after)


def test_decode_unverified_reads_content_anyway(keypair):
    """即使验签会失败，也要能读出「这码是给谁的」。

    否则用户只会看到一句「授权失败」，你和同事都无法定位问题。
    """
    code = make_code(keypair, fingerprint={})
    payload = codec.decode_unverified(code)
    assert payload.licensee == "张三"
    assert payload.machine == CODE_A
