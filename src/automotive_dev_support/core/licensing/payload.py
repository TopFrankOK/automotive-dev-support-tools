"""许可码载荷 —— 被签名的那个对象。

载荷是**完全公开**的：任何人都可以把它解出来看。
安全性不来自「藏起来」，而来自签名 —— 改任何一个字节，
签名就对不上。所以这里不需要混淆、不需要加密。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["LicensePayload", "PAYLOAD_VERSION"]

PAYLOAD_VERSION = 1


class LicensePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = PAYLOAD_VERSION
    """载荷版本。将来加字段时用它做兼容判断。"""

    lic_id: str
    """许可证编号，例如 `L-2026-0042`。用于对账与追溯。"""

    licensee: str = ""
    """被授权人姓名。"""

    machine: str = ""
    """展示用机器码，例如 `9F3A-51C7-D2E8-4B06`。
     它只用于「让用户和作者能对上号」，不参与校验。"""

    fp: dict[str, str] = Field(default_factory=dict)
    """本机各组件的指纹哈希，例如 `{"board": "...", "disk": "..."}`。
     这是真正参与校验的部分。"""

    fp_threshold: int = 1
    """要求命中多少个组件才算通过。
     默认 1，即「两项里命中一项即放行」——
     这是为了容忍换硬盘之类的单点硬件变更，避免用户被锁死。"""

    issued_at: datetime
    not_before: datetime
    expires_at: datetime

    grace_days: int = 0
    """到期后的宽限天数。宽限期内只读可用。
     车载标定常在试验场/车间，直接锁死会造成现场停工。"""

    budget_hours: float | None = None
    """累计运行小时配额。为 None 表示不限制。

     为什么要有它：用户在到期前把系统时间调回去就能一直用，
     日历约束会被绕过。但**已经消耗掉的运行时长无法被改小** ——
     它是按单调时钟累加的。这是离线场景下最有效的一道防线。"""

    features: list[str] = Field(default_factory=list)
    """授权的功能项。插件通过 manifest 的 `requires` 声明自己需要哪些，
     授权闸门据此决定是否放行。"""

    note: str = ""

    # -- 便捷查询 ----------------------------------------------------------

    def has_feature(self, feature: str) -> bool:
        return feature in self.features

    def days_left(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).total_seconds() / 86400

    def in_grace(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if not self.is_expired(now):
            return False
        return now < self.expires_at + timedelta(days=self.grace_days)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.not_before <= now < self.expires_at
