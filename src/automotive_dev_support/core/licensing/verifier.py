"""许可码校验 —— 五道关卡。

    ┌─ 1. 验签 ────────── 用内置公钥验证 Ed25519 签名
    ├─ 2. 机器指纹 ────── 命中数须达到阈值，防拷贝许可码
    ├─ 3. 防时钟回拨 ──── 当前时间不得早于历史最大已见时间
    ├─ 4. 有效期 ──────── not_before ~ expires_at（过期后进入宽限期）
    └─ 5. 运行时长预算 ── 按单调时钟累计，改系统日期无效

**顺序是刻意的。** 第 3 关排在第 4 关之前，因为如果系统时间被回拨，
第 4 关的结论本身就是不可信的 —— 先识别出时间不可信，
才不会用一个被操纵的 `now` 去判断「有没有过期」。

本模块是纯逻辑：不读文件、不碰注册表、不访问网络。
所有外部输入都通过 `LocalState` 传入。因此它可以被完整单测，
而「授权是否可信」这件事，前提就是它的判定逻辑可被验证。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, Field

from automotive_dev_support.core.licensing import codec
from automotive_dev_support.core.licensing.errors import (
    BadSignature,
    BudgetExhausted,
    ClockRollback,
    Expired,
    FeatureNotLicensed,
    MachineMismatch,
    MalformedCode,
    NotYetValid,
)
from automotive_dev_support.core.licensing.payload import LicensePayload

__all__ = ["LocalState", "VerifyOutcome", "LicenseVerifier", "DEFAULT_TOLERANCE_HOURS"]

DEFAULT_TOLERANCE_HOURS = 24.0
"""允许的时钟回拨容差。

必须留出余量：时区调整、夏令时、NTP 校正、用户手工改错，
都可能让系统时间短暂地往前跳一点点。
一律判死会造成大量误报，得不偿失。"""


@dataclass(frozen=True)
class LocalState:
    """本机的当前状况。所有外部输入都从这里进来，校验器本身不做 IO。"""

    machine_code: str
    fingerprint: dict[str, str] = field(default_factory=dict)
    max_seen: datetime | None = None
    """历史上见过的最大时间。由 platform 层多点冗余记录。"""

    used_hours: float = 0.0
    """累计已用运行小时。"""


class VerifyOutcome(BaseModel):
    payload: LicensePayload
    state: Literal["valid", "grace"] = "valid"
    days_left: float = 0.0
    matched_components: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def read_only(self) -> bool:
        """宽限期内降级为只读 —— 现场能查数据，但不能改标定。"""
        return self.state == "grace"


class LicenseVerifier:
    def __init__(
        self,
        public_key_b64: str,
        tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    ) -> None:
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise MalformedCode("内置公钥格式非法，软件包可能已损坏") from exc
        if len(raw) != 32:
            raise MalformedCode(f"Ed25519 公钥应为 32 字节，实际 {len(raw)} 字节")
        self._public_key = Ed25519PublicKey.from_public_bytes(raw)
        self._tolerance = timedelta(hours=tolerance_hours)

    # -- 主入口 ------------------------------------------------------------

    def verify(
        self,
        code: str,
        local: LocalState,
        feature: str | None = None,
        now: datetime | None = None,
    ) -> VerifyOutcome:
        now = now or datetime.now(timezone.utc)
        payload, signature, signed_bytes = codec.decode(code)

        matched = self._gate_signature(payload, signature, signed_bytes)
        matched_components = self._gate_machine(payload, local)
        self._gate_clock(payload, local, now)
        state = self._gate_validity(payload, now)
        self._gate_budget(payload, local)
        if feature is not None:
            self._gate_feature(payload, feature)

        return VerifyOutcome(
            payload=payload,
            state=state,
            days_left=payload.days_left(now),
            matched_components=matched_components,
            checked_at=now,
        )

    # -- 五道关卡 ----------------------------------------------------------

    def _gate_signature(
        self, payload: LicensePayload, signature: bytes, signed_bytes: bytes
    ) -> None:
        """第 1 关：验签。

        对**原始字节**验签，而不是对重新序列化后的对象 ——
        否则会因为字段顺序或时间格式的细微差别导致假失败。
        """
        try:
            self._public_key.verify(signature, signed_bytes)
        except InvalidSignature as exc:
            raise BadSignature("许可码签名无效") from exc
        if len(signature) != 64:
            raise BadSignature(f"Ed25519 签名应为 64 字节，实际 {len(signature)} 字节")

    def _gate_machine(self, payload: LicensePayload, local: LocalState) -> list[str]:
        """第 2 关：机器指纹比对，带容错。

        容错是刻意设计的：主板 UUID 与系统盘序列号两项里命中一项即通过。
        理由是换一块硬盘不该让人来重新申请许可 —— 那只会让你更麻烦。
        """
        if not payload.fp:
            # 兼容没有指纹字段的旧许可码，退回机器码比对
            if payload.machine and payload.machine != local.machine_code:
                raise MachineMismatch(
                    f"许可码绑定 {payload.machine}，本机为 {local.machine_code}"
                )
            return []

        matched = [
            name
            for name, expected in payload.fp.items()
            if local.fingerprint.get(name) == expected
        ]
        threshold = max(1, min(payload.fp_threshold, len(payload.fp)))
        if len(matched) < threshold:
            raise MachineMismatch(
                f"机器指纹未命中（需要 {threshold} 项，实际 {len(matched)} 项）。"
                f"本机机器码：{local.machine_code}"
            )
        return matched

    def _gate_clock(
        self, payload: LicensePayload, local: LocalState, now: datetime
    ) -> None:
        """第 3 关：防时钟回拨。

        这是「一个月有效期」最容易的绕过方式：把系统时间调到签发日之前。
        对策是记录历史最大已见时间（多点冗余），当前时间早于它就判异常。

        局限要说清楚：如果用户在拿到许可后**从未联网也从未启动过**，
        就调时间，理论上仍可延缓。所以还有第 5 关兜底。
        """
        if local.max_seen is None:
            return
        if now < local.max_seen - self._tolerance:
            raise ClockRollback(
                f"当前时间 {now:%Y-%m-%d %H:%M} 早于上次记录 "
                f"{local.max_seen:%Y-%m-%d %H:%M}"
            )

    def _gate_validity(
        self, payload: LicensePayload, now: datetime
    ) -> Literal["valid", "grace"]:
        """第 4 关：有效期。过期后若在宽限期内，降级为只读而不是直接锁死。"""
        if now < payload.not_before:
            raise NotYetValid(
                f"许可码将于 {payload.not_before:%Y-%m-%d} 生效"
            )
        if not payload.is_expired(now):
            return "valid"
        if payload.in_grace(now):
            return "grace"
        raise Expired(
            f"许可已于 {payload.expires_at:%Y-%m-%d} 到期"
            f"（宽限期 {payload.grace_days} 天）"
        )

    def _gate_budget(self, payload: LicensePayload, local: LocalState) -> None:
        """第 5 关：累计运行时长预算。

        这是离线场景下最有效的一道防线。日历可以被改，
        但**已经消耗掉的运行时长改不小** —— 它按单调时钟累加。
        用户把日期调回去，只会让预算更快耗尽。
        """
        if payload.budget_hours is None:
            return
        if local.used_hours > payload.budget_hours:
            raise BudgetExhausted(
                f"已用 {local.used_hours:.1f} 小时，配额 {payload.budget_hours:.1f} 小时"
            )

    def _gate_feature(self, payload: LicensePayload, feature: str) -> None:
        """功能项判定。

        `features` 为空的许可是**全功能授权** ——
        这样「只给一个月期限、不细分功能」的日常用法不必逐个列举功能名。
        需要按功能收费或分级时，再在签发时明确列出。
        """
        if not payload.features:
            return
        if not payload.has_feature(feature):
            raise FeatureNotLicensed(
                f"功能「{feature}」未包含在本许可中",
                hint=f"当前许可包含：{', '.join(payload.features)}",
            )
