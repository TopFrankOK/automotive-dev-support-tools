"""本地状态持久化：历史最大已见时间 + 累计运行时长。

## 为什么需要它

「有效期一个月」最容易被绕过的方式不是破解签名，而是**改系统时间**。
把时钟调回签发日之前，日历约束就失效了。

对策是记录「历史上见过的最大时间」，当前时间早于它就判异常。
但这份记录本身也会被删掉 —— 所以要**多点冗余**：

    1. %LOCALAPPDATA%\\AutomotiveDevSupport\\state.json
    2. 注册表 HKCU\\Software\\AutomotiveDevSupport
    3. 应用数据目录下的隐藏副本

读取时取各处的**最大值**（最保守），写入时全部更新。
要绕过就得同时找到并改对三处 —— 门槛足够高了。

## 诚实的边界说明

HMAC 标签用的是编译进程序的密钥，理论上可被逆向出来。
所以本模块防的是**随手删除、随手编辑**，不是有备而来的攻击者。
对团队内部工具来说这个强度是匹配的 —— 要防到那个程度，
应该上 CodeMeter，而不是继续加固这里。

同理，`used_hours` 用 `time.monotonic()` 累积：
它能对抗改系统日期，但对抗不了重启（单调时钟归零）。
所以由调用方定时调用 `add_hours()` 落盘，而不是只在退出时结算 ——
否则一次异常退出就会让整段运行时长凭空消失。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

__all__ = [
    "APP_DIR_NAME",
    "SeenState",
    "SeenClock",
    "StateStore",
    "FileStore",
    "RegistryStore",
    "default_stores",
    "align_to_utc",
]

APP_DIR_NAME = "AutomotiveDevSupport"
_REGISTRY_KEY = r"Software\AutomotiveDevSupport"
_REGISTRY_VALUE = "state"


class SeenState(BaseModel):
    """本地会话状态快照。"""

    max_seen: datetime | None = None
    """历史上见过的最大时间。防时钟回拨的核心依据。"""

    used_hours: float = 0.0
    """累计运行小时。按单调时钟累加，改系统日期改不小。"""

    launches: int = 0
    first_seen: datetime | None = None
    license_fingerprint: str = ""
    """当前激活许可码的指纹，用于识别状态是否属于同一张许可。"""

    def merged_with(self, other: SeenState) -> SeenState:
        """取各字段的「最保守」值。

        `max_seen` 与 `used_hours` 取最大 —— 攻击者无法通过在某一处
        写入一个很小的值来放宽约束。
        """
        times = [t for t in (self.max_seen, other.max_seen) if t is not None]
        starts = [t for t in (self.first_seen, other.first_seen) if t is not None]
        return SeenState(
            max_seen=max(times) if times else None,
            used_hours=max(self.used_hours, other.used_hours),
            launches=max(self.launches, other.launches),
            first_seen=min(starts) if starts else None,
            license_fingerprint=self.license_fingerprint or other.license_fingerprint,
        )


def align_to_utc(value: datetime) -> datetime:
    """统一成 UTC aware。历史记录里若写入过 naive 时间，在这里兜住。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class StateStore(Protocol):
    """一处存放位置。"""

    name: str

    def load(self) -> SeenState | None: ...

    def save(self, state: SeenState) -> None: ...

    def clear(self) -> None: ...


class _SignedStore:
    """带 HMAC 标签的 JSON 存储基类。"""

    name = "base"

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def _pack(self, state: SeenState) -> str:
        body = state.model_dump_json()
        tag = hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
        return json.dumps({"d": body, "t": tag}, ensure_ascii=False)

    def _unpack(self, text: str) -> SeenState | None:
        try:
            wrapper = json.loads(text)
            body = wrapper["d"]
            tag = wrapper["t"]
        except (ValueError, KeyError, TypeError):
            return None
        expected = hmac.new(
            self._secret, body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(tag, expected):
            # 标签不对说明被手工改过。**当作「不存在」处理而不是报错** ——
            # 用户可能只是清了缓存，不该因此连启动都不行。
            return None
        try:
            return SeenState.model_validate_json(body)
        except ValueError:
            return None


class FileStore(_SignedStore):
    def __init__(self, path: Path, secret: bytes, name: str = "file") -> None:
        super().__init__(secret)
        self.path = path
        self.name = name

    def load(self) -> SeenState | None:
        try:
            return self._unpack(self.path.read_text("utf-8"))
        except OSError:
            return None

    def save(self, state: SeenState) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.parent / (self.path.name + ".tmp")
            tmp.write_text(self._pack(state), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


class RegistryStore(_SignedStore):
    name = "registry"

    def load(self) -> SeenState | None:
        try:
            import winreg  # noqa: PLC0415 - 仅 Windows 存在
        except ImportError:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY) as key:
                value, _ = winreg.QueryValueEx(key, _REGISTRY_VALUE)
            return self._unpack(str(value))
        except OSError:
            return None

    def save(self, state: SeenState) -> None:
        try:
            import winreg  # noqa: PLC0415
        except ImportError:
            return
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY) as key:
                winreg.SetValueEx(
                    key, _REGISTRY_VALUE, 0, winreg.REG_SZ, self._pack(state)
                )
        except OSError:
            pass

    def clear(self) -> None:
        try:
            import winreg  # noqa: PLC0415
        except ImportError:
            return
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, _REGISTRY_VALUE)
        except OSError:
            pass


def default_stores(secret: bytes, data_dir: Path | None = None) -> list[StateStore]:
    """默认的三处冗余位置。"""
    base = data_dir or (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".automotive_dev_support")
        / APP_DIR_NAME
    )
    return [
        FileStore(base / "state.json", secret, name="localappdata"),
        RegistryStore(secret),
        FileStore(base / ".state.bak", secret, name="hidden"),
    ]


class SeenClock:
    """历史已见时间与累计使用时长的看门人。"""

    def __init__(self, stores: list[StateStore]) -> None:
        self._stores = stores
        self._cache: SeenState | None = None
        self._session_start: float | None = None

    # -- 读写 --------------------------------------------------------------

    def load(self) -> SeenState:
        """读取并合并所有位置。合并结果会缓存，避免反复读注册表。"""
        if self._cache is not None:
            return self._cache
        merged: SeenState | None = None
        for store in self._stores:
            try:
                state = store.load()
            except Exception:  # noqa: BLE001 - 单处故障不影响其它位置
                continue
            if state is None:
                continue
            merged = state if merged is None else merged.merged_with(state)
        self._cache = merged or SeenState()
        return self._cache

    def save(self, state: SeenState) -> None:
        self._cache = state
        for store in self._stores:
            try:
                store.save(state)
            except Exception:  # noqa: BLE001
                continue

    def clear(self) -> None:
        """清空所有位置。仅供测试与「重置授权」使用。"""
        self._cache = None
        for store in self._stores:
            try:
                store.clear()
            except Exception:  # noqa: BLE001
                continue

    # -- 更新 --------------------------------------------------------------

    def touch(
        self, now: datetime | None = None, license_fingerprint: str = ""
    ) -> SeenState:
        """记录「现在被看到过」，并累加启动次数。"""
        now = now or datetime.now(timezone.utc)
        state = self.load().model_copy(deep=True)
        if state.max_seen is None or now > align_to_utc(state.max_seen):
            state.max_seen = now
        state.launches += 1
        if state.first_seen is None:
            state.first_seen = now
        if license_fingerprint:
            state.license_fingerprint = license_fingerprint
        self.save(state)
        return state

    def add_hours(self, hours: float) -> SeenState:
        """累加运行时长。非正数直接忽略。"""
        if hours <= 0:
            return self.load()
        state = self.load().model_copy(deep=True)
        state.used_hours += hours
        self.save(state)
        return state

    # -- 会话计量 ----------------------------------------------------------

    def begin(self) -> None:
        """开始计量一次运行。用单调时钟，改系统日期无法影响它。"""
        import time  # noqa: PLC0415

        self._session_start = time.monotonic()

    def heartbeat(self) -> SeenState:
        """把自上次结算以来消耗的时长落盘。崩溃前调用也有效。

        建议调用方每 5 分钟调一次 —— 只靠退出时结算的话，
        一次异常退出会让整段运行时长凭空消失。
        """
        import time  # noqa: PLC0415

        if self._session_start is None:
            return self.load()
        now = time.monotonic()
        elapsed = now - self._session_start
        self._session_start = now
        return self.add_hours(elapsed / 3600.0)

    def end(self) -> SeenState:
        state = self.heartbeat()
        self._session_start = None
        return state

    @contextmanager
    def session(self) -> Iterator[SeenState]:
        """便捷上下文：进入时 touch 并开始计时，退出时结算时长。

        注意：不包含定时心跳。长时间运行且需要抗崩溃的场景，
        应当显式使用 `begin()` + 定期 `heartbeat()`。
        """
        state = self.touch()
        self.begin()
        try:
            yield state
        finally:
            self.end()
