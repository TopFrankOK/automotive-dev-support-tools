"""签发端 —— 只有作者会用到这部分。

放在 core 里（而不是 tools 里）是刻意的：签发与校验必须共用
同一份 `canonical_bytes`。一旦这两处的序列化逻辑出现任何差异，
就会产生「明明是我签的却验不过」这类极难排查的问题。
共用一份实现是唯一可靠的做法。

**私钥永远不要进入这个仓库，也永远不要打包进发布版。**
`generate_keypair` 只在 `issuer init` 时跑一次，
之后私钥应存到离线 U 盘或密码管理器。
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from automotive_dev_support.core.licensing import codec
from automotive_dev_support.core.licensing.payload import LicensePayload

__all__ = [
    "generate_keypair",
    "private_key_from_hex",
    "public_key_b64",
    "sign_payload",
    "issue",
]


def generate_keypair() -> tuple[str, str]:
    """生成密钥对，返回 `(私钥 hex, 公钥 base64)`。

    公钥 base64 会被硬编码进软件；私钥 hex 必须离线保管。
    """
    private = Ed25519PrivateKey.generate()
    priv_hex = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    return priv_hex, public_key_b64(private)


def private_key_from_hex(priv_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex.strip()))


def public_key_b64(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def sign_payload(payload: LicensePayload, private: Ed25519PrivateKey) -> str:
    """签名并装配成许可码。"""
    signature = private.sign(codec.canonical_bytes(payload))
    return codec.encode(payload, signature)


def issue(
    private: Ed25519PrivateKey,
    *,
    lic_id: str,
    licensee: str,
    machine_code: str,
    fingerprint: dict[str, str],
    days: int = 30,
    features: list[str] | None = None,
    budget_hours: float | None = None,
    grace_days: int = 7,
    fp_threshold: int = 1,
    not_before: datetime | None = None,
    note: str = "",
    expire_from: datetime | None = None,
    now: datetime | None = None,
) -> tuple[str, LicensePayload]:
    """签发一张许可码。

    `now` 让签发端也接受注入的时钟。这不是为了测试方便而已 ——
    **签发端与校验端必须对称**：校验端已经能接受 `now`，
    签发端若只能读墙钟，「有效期 30 天」这类判定就只能依赖
    跑测试的那一刻，同一个用例在不同时间点会给出不同结论。
    注入时钟之后，整条授权链路才是可复现的。

    `expire_from` 用于**提前续期**：把新到期日设为「旧到期日 + N 天」，
    这样同事提前一周来续期也不会损失剩余天数，作者也不用手工记账。
    """
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    start = not_before or now
    base = expire_from or start
    payload = LicensePayload(
        lic_id=lic_id,
        licensee=licensee,
        machine=machine_code,
        fp=fingerprint,
        fp_threshold=fp_threshold,
        issued_at=now,
        not_before=start,
        expires_at=base + timedelta(days=days),
        grace_days=grace_days,
        budget_hours=budget_hours,
        features=features or [],
        note=note,
    )
    return sign_payload(payload, private), payload
