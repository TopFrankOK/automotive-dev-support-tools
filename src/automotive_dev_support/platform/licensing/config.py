"""授权配置：内置公钥与运行目录。

## 公钥从哪来

作者在**自己机器上跑一次**：

    python tools/license-issuer/issuer.py init

它会生成密钥对，并把公钥打印出来。把公钥粘贴到下面的
`PUBLIC_KEY_B64`，然后重新打包。这一步做完就再也不需要动了，
以后只需用私钥签发许可码。

## 为什么公钥可以硬编码

公钥只能**验证**签名，不能**生成**签名。
即使有人把它抠出来，他也造不出一张新许可码。
所以把它写死在代码里是安全的 —— 这是签名式授权的关键性质。

反过来，**私钥绝不能出现在这个仓库的任何位置**。
它一旦泄露，整套授权体系即刻失效。`issuer init` 生成的私钥
应当立刻转移到离线 U 盘或密码管理器。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "APP_NAME",
    "APP_ID",
    "PUBLIC_KEY_B64",
    "PUBLIC_KEY_CONFIGURED",
    "default_data_dir",
    "default_license_dir",
    "require_public_key",
]

APP_NAME = "Automotive Software Development Support Tools"
APP_ID = "automotive_dev_support"

#: 由 `issuer init` 生成后粘贴到这里。
#: 未配置时软件仍可启动，但授权功能会明确报错而不是「静默放行」。
PUBLIC_KEY_B64 = os.environ.get("WORKFRANKLIN_PUBLIC_KEY", "")

PUBLIC_KEY_CONFIGURED = bool(PUBLIC_KEY_B64)

_PLACEHOLDER_HINT = (
    "内置公钥尚未配置。请先在作者机器上运行 "
    "`python tools/license-issuer/issuer.py init`，"
    "把输出的公钥粘贴到 src/automotive_dev_support/platform/licensing/config.py 的 "
    "PUBLIC_KEY_B64，然后重新打包。"
)


def default_data_dir() -> Path:
    """应用数据目录（状态记录与本地许可码都放这里）。"""
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def default_license_dir() -> Path:
    return default_data_dir()


def require_public_key() -> str:
    """取公钥；未配置时抛出可读的错误。

    刻意**不**在未配置时退化成「无授权也能用」——
    那会让一次打包疏忽变成整个授权体系的静默失效。
    """
    from automotive_dev_support.core.licensing.errors import LicenseError  # noqa: PLC0415

    if not PUBLIC_KEY_CONFIGURED:
        raise LicenseError(_PLACEHOLDER_HINT, hint=_PLACEHOLDER_HINT)
    return PUBLIC_KEY_B64
