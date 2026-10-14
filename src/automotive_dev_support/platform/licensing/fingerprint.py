"""机器指纹采集（Windows）。

## 为什么用「指纹 + 容错」而不是「一把死锁」

如果只取一个硬件标识并严格比对，那么换一块硬盘、重装一次系统，
用户就会被彻底锁死，然后来找你 —— 这恰好是你想避免的麻烦。

所以这里采集**多个稳定组件**，各自独立哈希，由校验器按阈值判定。
默认阈值 1，即「两项里命中一项就放行」。
代价是略微宽松，换来的是日常几乎不会有人因为硬件变动来找你。

## 组件选择原则

选**稳定**的，不选**方便**的：

    主板 UUID（Win32_ComputerSystemProduct.UUID）  最稳定，作为主标识
    系统盘序列号                                    换硬盘才变
    Windows MachineGuid（注册表）                   重装系统才变

刻意**不用**：
    MAC 地址      多网卡、虚拟机、网卡驱动重装都会变，误报率高
    CPU ID        虚拟化环境下不稳定
    计算机名      用户随手就能改

## 一个必须处理的坑

不少主板厂商在 SMBIOS 里填的是占位字符串 ——
`To be filled by O.E.M.`、`Default string`、`00000000-0000-...`、
空串。这些值在成千上万台机器上完全一样。
如果不过滤，你的「机器指纹」会变成一把万能钥匙。
`_normalize` 就是为了处理这件事，**不要删掉它**。
"""

from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import uuid
from functools import lru_cache

__all__ = [
    "COMPONENT_BOARD",
    "COMPONENT_DISK",
    "COMPONENT_SYS",
    "collect_components",
    "fingerprint",
    "machine_code",
    "format_machine_code",
]

COMPONENT_BOARD = "board"
COMPONENT_DISK = "disk"
COMPONENT_SYS = "sys"

#: SMBIOS / 注册表里的占位值。命中则视为「本组件不可用」。
_PLACEHOLDERS = {
    "",
    "NONE",
    "NOTSPECIFIED",
    "NOTAPPLICABLE",
    "TOBEFILLEDBYO.E.M.",
    "TOBEFILLEDBYOEM",
    "DEFAULTSTRING",
    "SYSTEMMANUFACTURER",
    "SYSTEMPRODUCTNAME",
    "SYSTEMVERSION",
    "INVALID",
    "UNKNOWN",
    "00000000-0000-0000-0000-000000000000",
    "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
    "0",
}

_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cs = Get-CimInstance -ClassName Win32_ComputerSystemProduct
if ($cs) { Write-Output ("BOARD=" + $cs.UUID) }
$disks = Get-CimInstance -ClassName Win32_DiskDrive | Sort-Object Index | Select-Object -First 3
foreach ($d in $disks) {
    if ($d.SerialNumber) { Write-Output ("DISK=" + $d.SerialNumber.Trim()) }
}
$bb = Get-CimInstance -ClassName Win32_BaseBoard
if ($bb) { Write-Output ("BASEBOARD=" + $bb.SerialNumber) }
"""


def _normalize(value: str | None) -> str:
    """归一化并过滤占位值。返回空串表示该组件不可用。"""
    if not value:
        return ""
    text = re.sub(r"\s+", "", value).upper()
    if text in _PLACEHOLDERS:
        return ""
    # 形如 00000000-0000-0000-0000-000000000000 的 UUID
    if re.fullmatch(r"0{8}-0{4}-0{4}-0{4}-0{12}", text):
        return ""
    if set(text) <= {"0"}:
        return ""
    return text


def _run_powershell(script: str, timeout_s: float = 8.0) -> str:
    """调用 PowerShell。

    只调用**一次**并把所需字段一次性取回 —— PowerShell 冷启动要几百毫秒，
    分三次调用会让软件启动明显变慢。
    """
    for exe in ("powershell.exe", "pwsh.exe"):
        try:
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.decode("utf-8", errors="replace")
    return ""


def _from_registry() -> str:
    """Windows MachineGuid。注册表读取比起进程快得多。"""
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 存在
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except OSError:
        return ""


@lru_cache(maxsize=1)
def collect_components() -> dict[str, str]:
    """采集各组件的原始值。缺失的组件不会出现在结果里。

    结果做了进程内缓存 —— 指纹在一次运行内不会变，
    而 PowerShell 调用是这里唯一有明显开销的操作。
    """
    raw: dict[str, str] = {}

    output = _run_powershell(_PS_SCRIPT)
    disks: list[str] = []
    for line in output.splitlines():
        key, _, value = line.partition("=")
        key = key.strip().upper()
        if key == "BOARD":
            if (norm := _normalize(value)) and COMPONENT_BOARD not in raw:
                raw[COMPONENT_BOARD] = norm
        elif key == "DISK":
            if norm := _normalize(value):
                disks.append(norm)
        elif key == "BASEBOARD":
            # 主板序列号是 UUID 缺失时的替补，不单独作为一个组件
            if norm and COMPONENT_BOARD not in raw:
                raw[COMPONENT_BOARD] = norm

    if disks:
        # 多盘时合并而不是取第一块：换掉非系统盘不应影响指纹
        raw[COMPONENT_DISK] = "|".join(sorted(disks))

    if norm := _normalize(_from_registry()):
        raw[COMPONENT_SYS] = norm

    # 兜底：一个组件都取不到时，至少让指纹不是空的
    # （这种情况只出现在极度受限的环境，会牺牲稳定性）
    if not raw:
        fallback = _normalize(f"{platform.node()}") or "FALLBACK"
        raw[COMPONENT_BOARD] = fallback

    return raw


@lru_cache(maxsize=1)
def fingerprint() -> dict[str, str]:
    """`{组件名: 16 位哈希}`。"""

    def _hash(name: str, value: str) -> str:
        return hashlib.sha256(f"{name}:{value}".encode("utf-8")).hexdigest()[:16]

    return {name: _hash(name, value) for name, value in collect_components().items()}


@lru_cache(maxsize=1)
def machine_code() -> str:
    """给用户看的机器码，形如 `9F3A-51C7-D2E8-4B06`。

    刻意**从主板指纹派生**而不是从全部组件的拼接派生：
    换硬盘时机器码保持不变，用户不必重新报码给你。
    """
    fp = fingerprint()
    primary = fp.get(COMPONENT_BOARD) or next(iter(fp.values()), "")
    digest = hashlib.sha256(primary.encode("utf-8")).hexdigest()[:16].upper()
    return format_machine_code(digest)


def format_machine_code(digest: str) -> str:
    """`9f3a51c7d2e84b06` → `9F3A-51C7-D2E8-4B06`

    分组是为了让人能抄、能念、能截图 ——
    一串 16 位连续字符在电话里报是很容易出错的。
    """
    text = digest.upper().replace("-", "")
    return "-".join(text[i : i + 4] for i in range(0, min(len(text), 16), 4))


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print("机器码:", machine_code())
    for key, value in fingerprint().items():
        print(f"  {key:>6}: {value}")
    for key, value in collect_components().items():
        print(f" 原始 {key:>6}: {value}")
    print("平台:", platform.platform())
    print("节点:", uuid.getnode())
