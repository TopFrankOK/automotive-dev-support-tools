"""授权相关的错误类型。

每一种失败都有独立的异常类型，而不是笼统抛 `LicenseError`。
原因是：**界面需要根据失败原因给出不同的引导**。

    BadSignature      → "许可码不完整，可能复制时漏了字符"（让用户重新粘贴）
    MachineMismatch   → "本机机器码是 XXX，请把它发给作者"（引导重新申请）
    Expired           → "已过期，请把机器码 XXX 发给作者续期"
    ClockRollback     → "系统时间异常，请检查时间设置"（不是让用户找作者）

如果只有一个异常类型，这些引导就全部塌缩成一句无用的「授权失败」。
"""

from __future__ import annotations

__all__ = [
    "LicenseError",
    "MalformedCode",
    "BadSignature",
    "MachineMismatch",
    "NotYetValid",
    "Expired",
    "ClockRollback",
    "BudgetExhausted",
    "FeatureNotLicensed",
    "NotActivated",
]


class LicenseError(Exception):
    """授权校验失败基类。"""

    code: str = "license_error"
    hint: str = ""

    def __init__(self, message: str = "", hint: str = "") -> None:
        super().__init__(message or self.__class__.__doc__ or "")
        self.message = message or str(self)
        if hint:
            self.hint = hint

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class NotActivated(LicenseError):
    """尚未激活。"""

    code = "not_activated"
    hint = "请把本机机器码发给作者获取许可码，然后在激活框中粘贴。"


class MalformedCode(LicenseError):
    """许可码格式非法。"""

    code = "malformed_code"
    hint = "许可码可能复制不完整，请重新完整复制一次（不要手动输入）。"


class BadSignature(LicenseError):
    """签名校验未通过。"""

    code = "bad_signature"
    hint = "许可码不是由本软件的作者签发的，或复制过程中被改动过。"


class MachineMismatch(LicenseError):
    """机器指纹不匹配。"""

    code = "machine_mismatch"
    hint = "本许可码绑定的不是这台电脑。请把本机机器码发给作者重新签发。"


class NotYetValid(LicenseError):
    """许可码尚未生效。"""

    code = "not_yet_valid"


class Expired(LicenseError):
    """许可码已过期。"""

    code = "expired"
    hint = "请把本机机器码发给作者续期。"


class ClockRollback(LicenseError):
    """检测到系统时间被回拨。"""

    code = "clock_rollback"
    hint = "本机系统时间早于上次使用记录。请检查系统时间设置是否正确。"


class BudgetExhausted(LicenseError):
    """累计运行时长已用尽。"""

    code = "budget_exhausted"
    hint = "本许可的累计使用时长已用完，请把本机机器码发给作者续期。"


class FeatureNotLicensed(LicenseError):
    """该功能未包含在当前许可中。"""

    code = "feature_not_licensed"
