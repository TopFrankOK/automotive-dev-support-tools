"""授权服务 —— 把指纹采集、状态存储、校验串起来。

`core.licensing` 是纯逻辑，本模块是它唯一的「接线员」。
后端（FastAPI 的授权闸门）和 CLI 都只跟它打交道，
这样「授权状态从哪来」这件事只有一处答案。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from automotive_dev_support.core.licensing import (
    LicenseError,
    LicensePayload,
    LicenseVerifier,
    LocalState,
    VerifyOutcome,
    decode_unverified,
)
from automotive_dev_support.core.licensing.errors import FeatureNotLicensed, NotActivated
from automotive_dev_support.platform.licensing.fingerprint import (
    fingerprint as collect_fingerprint,
    machine_code as collect_machine_code,
)
from automotive_dev_support.platform.licensing.store import SeenClock, StateStore, default_stores

__all__ = ["LicenseStatus", "LicensingService", "LICENSE_FILE_NAME"]

LICENSE_FILE_NAME = "license.code"

#: 状态存储 HMAC 密钥的派生盐。改它会作废所有已有的本地状态记录。
_STATE_SALT = b"AutomotiveDevSupport.state.v1"

LicenseStateName = Literal[
    "not_activated", "valid", "grace", "expired", "invalid", "error"
]


class LicenseStatus(BaseModel):
    """给界面看的一份完整授权状态。不可序列化问题之外，字段都是自解释的。"""

    state: LicenseStateName = "not_activated"
    machine_code: str = ""
    lic_id: str = ""
    licensee: str = ""
    features: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    days_left: float = 0.0
    grace_days: int = 0
    used_hours: float = 0.0
    budget_hours: float | None = None
    read_only: bool = False
    message: str = ""
    hint: str = ""

    @property
    def usable(self) -> bool:
        return self.state in ("valid", "grace")

    def to_display(self) -> str:
        """一行摘要，CLI 与日志共用。"""
        match self.state:
            case "valid":
                return f"有效 · {self.licensee} · 剩余 {self.days_left:.1f} 天"
            case "grace":
                return f"已过期，宽限期内（只读）· 请尽快续期"
            case "expired":
                return "已过期"
            case "not_activated":
                return f"未激活 · 机器码 {self.machine_code}"
            case "invalid":
                return f"许可无效：{self.message}"
            case _:
                return f"授权异常：{self.message}"


class LicensingService:
    """授权的唯一对外入口。"""

    def __init__(
        self,
        public_key_b64: str,
        data_dir: Path | None = None,
        tolerance_hours: float | None = None,
        stores: list[StateStore] | None = None,
    ) -> None:
        self._verifier = (
            LicenseVerifier(public_key_b64)
            if tolerance_hours is None
            else LicenseVerifier(public_key_b64, tolerance_hours)
        )
        self._data_dir = data_dir
        # `stores` 可注入，测试里用它避开真实注册表 ——
        # 单测不应有副作用，这是硬要求。
        self._clock = SeenClock(
            stores if stores is not None else default_stores(_STATE_SALT, data_dir)
        )

    @classmethod
    def from_config(cls, data_dir: Path | None = None) -> LicensingService:
        """用硬编码的内置公钥构建。生产路径都走这个。

        公钥未配置时明确抛错，**不退化成「无授权也能用」**——
        那会让一次打包疏忽变成整个授权体系的静默失效。
        """
        from automotive_dev_support.platform.licensing.config import require_public_key

        return cls(require_public_key(), data_dir)

    # -- 基础信息 ----------------------------------------------------------

    @staticmethod
    def machine_code() -> str:
        return collect_machine_code()

    @staticmethod
    def fingerprint() -> dict[str, str]:
        return collect_fingerprint()

    @property
    def clock(self) -> SeenClock:
        return self._clock

    def _license_path(self) -> Path:
        base = self._data_dir or self._default_dir()
        return base / LICENSE_FILE_NAME

    @staticmethod
    def _default_dir() -> Path:
        import os  # noqa: PLC0415

        return Path(
            os.environ.get("LOCALAPPDATA") or Path.home() / ".automotive_dev_support"
        ) / "AutomotiveDevSupport"

    # -- 读取状态 ----------------------------------------------------------

    def current_code(self) -> str | None:
        try:
            text = self._license_path().read_text("utf-8").strip()
        except OSError:
            return None
        return text or None

    def status(self, feature: str | None = None) -> LicenseStatus:
        """完整状态。**永不抛异常** —— 界面随时调用它都不会崩。"""
        machine = self.machine_code()
        seen = self._clock.load()
        code = self.current_code()
        if code is None:
            return LicenseStatus(
                state="not_activated",
                machine_code=machine,
                used_hours=seen.used_hours,
                hint=NotActivated.hint,
            )

        try:
            outcome = self._verify(code, seen, feature)
        except LicenseError as exc:
            return self._status_from_error(exc, machine, code, seen)

        return self._status_from_outcome(outcome, machine, seen)

    def _status_from_outcome(
        self, outcome: VerifyOutcome, machine: str, seen: object
    ) -> LicenseStatus:
        payload = outcome.payload
        return LicenseStatus(
            state=outcome.state,
            machine_code=machine,
            lic_id=payload.lic_id,
            licensee=payload.licensee,
            features=list(payload.features),
            expires_at=payload.expires_at,
            days_left=outcome.days_left,
            grace_days=payload.grace_days,
            used_hours=getattr(seen, "used_hours", 0.0),
            budget_hours=payload.budget_hours,
            read_only=outcome.read_only,
        )

    def _status_from_error(
        self, exc: LicenseError, machine: str, code: str, seen: object
    ) -> LicenseStatus:
        """失败时仍尽量把载荷里的信息显示出来。

        这样用户才能看到「这张码是签给谁的、什么时候到期」，
        从而和作者对上号 —— 否则只有一句「授权失败」，谁也帮不了他。
        """
        base = LicenseStatus(
            state="invalid",
            machine_code=machine,
            used_hours=getattr(seen, "used_hours", 0.0),
            message=str(exc),
            hint=exc.hint,
        )
        try:
            payload = decode_unverified(code)
        except LicenseError:
            return base
        base.lic_id = payload.lic_id
        base.licensee = payload.licensee
        base.expires_at = payload.expires_at
        base.features = list(payload.features)
        base.grace_days = payload.grace_days
        base.budget_hours = payload.budget_hours
        if exc.code == "expired":
            base.state = "expired"
            base.days_left = payload.days_left()
        return base

    # -- 激活与注销 --------------------------------------------------------

    def activate(self, code: str, feature: str | None = None) -> LicenseStatus:
        """验证并保存许可码。

        先做完整校验再落盘 —— 保存一张验证不通过的许可码，
        只会让之后每次启动都失败，且用户不知道自己什么时候弄坏的。
        """
        seen = self._clock.load()
        outcome = self._verify(code, seen, feature)  # 失败会抛 LicenseError
        path = self._license_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code.strip(), encoding="utf-8")
        # 记录这张许可的指纹，并强制刷新 max_seen
        self._clock.touch(license_fingerprint=self._code_fingerprint(code))
        return self._status_from_outcome(outcome, self.machine_code(), seen)

    def deactivate(self) -> None:
        """删除本地许可码。状态记录保留 —— 否则用户可以通过「注销」重置时长。"""
        try:
            self._license_path().unlink(missing_ok=True)
        except OSError:
            pass

    # -- 供业务层使用 ------------------------------------------------------

    def require(self, feature: str | None = None) -> VerifyOutcome:
        """需要授权的代码路径调用它。失败时抛 `LicenseError`。

        后端把它包成 FastAPI 的全局依赖挂到所有业务路由上，
        这样新加的任何接口都自动受保护，不会漏。
        """
        code = self.current_code()
        if code is None:
            raise NotActivated("软件尚未激活", hint=NotActivated.hint)
        seen = self._clock.load()
        return self._verify(code, seen, feature)

    def require_mutable(self) -> VerifyOutcome:
        """需要**写入**的路径调用它。宽限期内会拒绝。

        宽限期只给只读能力：现场能查数据、能看比对结果，
        但不能改标定、不能刷写 —— 既不至于让同事停工，
        也不至于让过期许可继续产出可交付的工程数据。
        """
        outcome = self.require()
        if outcome.read_only:
            raise FeatureNotLicensed(
                "当前许可已过期，处于只读宽限期，无法执行修改操作",
                hint="请把本机机器码发给作者续期。",
            )
        return outcome

    def granted_features(self) -> list[str]:
        """当前已授权的功能项。注册表用它判断哪些插件可用。"""
        status = self.status()
        return status.features if status.usable else []

    # -- 内部 --------------------------------------------------------------

    def _verify(
        self, code: str, seen: object, feature: str | None
    ) -> VerifyOutcome:
        state = LocalState(
            machine_code=self.machine_code(),
            fingerprint=self.fingerprint(),
            max_seen=getattr(seen, "max_seen", None),
            used_hours=getattr(seen, "used_hours", 0.0),
        )
        return self._verifier.verify(code, state, feature)

    @staticmethod
    def _code_fingerprint(code: str) -> str:
        return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()[:16]
