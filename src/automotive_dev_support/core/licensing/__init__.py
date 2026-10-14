"""授权核心 —— 纯逻辑，无 IO。

本包只做三件事：编解码许可码、签发、校验。
机器指纹采集、状态持久化、与界面交互，全部在 `platform.licensing`。

这样切分的原因很实际：授权是唯一「判定错了就意味着业务失败」的模块。
把它做成纯函数式的，就能用单元测试覆盖每一条分支 ——
而这正是它能被信任的前提。

本包对第三方的依赖只有 `cryptography`（纯密码学原语，无 IO）。
"""

from automotive_dev_support.core.licensing.codec import decode, decode_unverified, encode
from automotive_dev_support.core.licensing.errors import (
    BadSignature,
    BudgetExhausted,
    ClockRollback,
    Expired,
    FeatureNotLicensed,
    LicenseError,
    MachineMismatch,
    MalformedCode,
    NotActivated,
    NotYetValid,
)
from automotive_dev_support.core.licensing.payload import PAYLOAD_VERSION, LicensePayload
from automotive_dev_support.core.licensing.signing import (
    generate_keypair,
    issue,
    private_key_from_hex,
    public_key_b64,
    sign_payload,
)
from automotive_dev_support.core.licensing.verifier import (
    DEFAULT_TOLERANCE_HOURS,
    LicenseVerifier,
    LocalState,
    VerifyOutcome,
)

__all__ = [
    # codec
    "encode",
    "decode",
    "decode_unverified",
    # payload
    "LicensePayload",
    "PAYLOAD_VERSION",
    # signing
    "generate_keypair",
    "private_key_from_hex",
    "public_key_b64",
    "sign_payload",
    "issue",
    # verifying
    "LicenseVerifier",
    "LocalState",
    "VerifyOutcome",
    "DEFAULT_TOLERANCE_HOURS",
    # errors
    "LicenseError",
    "NotActivated",
    "MalformedCode",
    "BadSignature",
    "MachineMismatch",
    "NotYetValid",
    "Expired",
    "ClockRollback",
    "BudgetExhausted",
    "FeatureNotLicensed",
]
