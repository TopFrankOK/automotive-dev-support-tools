"""授权基础设施：指纹采集、状态持久化、以及把二者接到校验逻辑上。

分工：
    fingerprint.py  采集 Windows 机器指纹（主板 UUID / 系统盘序列号 / MachineGuid）
    store.py        多点冗余记录「历史最大已见时间」与「累计运行时长」
    service.py      上面两者 + `core.licensing` 的接线员，对外唯一入口

导入顺序有讲究：`fingerprint` 与 `store` 必须先于 `service` 载入，
避免 service 在包初始化途中引用尚未就位的子模块。
"""

from automotive_dev_support.platform.licensing import fingerprint, store  # noqa: F401
from automotive_dev_support.platform.licensing.fingerprint import (
    collect_components,
    fingerprint as machine_fingerprint,
    format_machine_code,
    machine_code,
)
from automotive_dev_support.platform.licensing.service import LICENSE_FILE_NAME, LicenseStatus, LicensingService
from automotive_dev_support.platform.licensing.store import (
    FileStore,
    RegistryStore,
    SeenClock,
    SeenState,
    default_stores,
)

__all__ = [
    # fingerprint
    "machine_code",
    "machine_fingerprint",
    "collect_components",
    "format_machine_code",
    # store
    "SeenClock",
    "SeenState",
    "FileStore",
    "RegistryStore",
    "default_stores",
    # service
    "LicensingService",
    "LicenseStatus",
    "LICENSE_FILE_NAME",
]
