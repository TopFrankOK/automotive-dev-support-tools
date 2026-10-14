"""各领域模型共享的最小内核。

刻意保持极小。这里只放**确实被多个领域同时需要、且语义完全一致**
的东西。一旦发现某个类型只被一个领域使用，就应该搬回那个领域 ——
共享内核变胖是架构腐化的开始。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

__all__ = ["SymbolPath", "sha256_hex", "now_utc"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(data: bytes, length: int = 16) -> str:
    """短哈希。用于内容指纹与缓存键，不作为密码学用途。"""
    return hashlib.sha256(data).hexdigest()[:length]


class SymbolPath(BaseModel):
    """符号路径 —— 全系统最重要的一条设计约定。

    **跨版本对齐必须靠符号路径，不能靠地址。**

    原因：App 编译一次，符号地址就可能整体漂移几十字节。
    如果拿地址当标识去比对两个版本的标定数据，你会得到满屏假差异，
    而真正的改动淹没其中。符号路径（`EngCal.Ctrl.PID.Kp`）是稳定的，
    地址只是它的一个会变的属性。

    这也是 CANape CDM 的核心价值所在。
    """

    model_config = ConfigDict(frozen=True)

    segments: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> SymbolPath:
        """支持 `A.B.C`、`A/B/C`、`A\\B\\C` 三种分隔风格。"""
        normalized = text.replace("/", ".").replace("\\", ".")
        return cls(segments=tuple(s for s in normalized.split(".") if s))

    @property
    def text(self) -> str:
        return ".".join(self.segments)

    @property
    def name(self) -> str:
        """末段，通常是显示名。"""
        return self.segments[-1] if self.segments else ""

    @property
    def parent(self) -> SymbolPath | None:
        return SymbolPath(segments=self.segments[:-1]) if len(self.segments) > 1 else None

    @property
    def depth(self) -> int:
        return len(self.segments)

    def startswith(self, other: SymbolPath) -> bool:
        return self.segments[: len(other.segments)] == other.segments

    def __str__(self) -> str:
        return self.text

    def __lt__(self, other: SymbolPath) -> bool:  # 便于排序输出
        return self.text < other.text
