"""数据源抽象 —— 统一骨架的第二根柱子。

所有「数据的来处」都归一到这里：本地文件、总线、诊断设备、
外部工具、版本库。对上层而言，它们只有 `capabilities()` 的差别。

这个抽象买到的东西很具体：
  - 新增一种数据来源（比如某个厂商的私有格式）= 加一个实现，不改上层
  - 上层可以用 `capabilities()` 决定策略，而不是用 `isinstance` 判断类型
    （例：有 STREAM 才允许打开 GB 级文件；有 WRITE 才显示「保存」按钮）

注意这里只定义**数据访问**。在线会话（连接、超时、重试、断点续刷）
属于 `core.ports` 里的 Gateway，不要塞进数据源。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from automotive_dev_support.core.skeleton.artifact import ArtifactKind, ArtifactRef
from automotive_dev_support.core.skeleton.capability import SourceCapability

__all__ = ["DataSource", "SourceError", "ReadOnlySourceError", "NotSeekableError"]


class SourceError(Exception):
    """数据源层面的错误基类。"""


class ReadOnlySourceError(SourceError):
    """试图向一个未声明 WRITE 能力的数据源写入。"""


class NotSeekableError(SourceError):
    """试图在一个未声明 SEEK 能力的数据源上随机访问。"""


class DataSource(ABC):
    """数据的来处。

    实现者必须保证：`capabilities()` 返回的能力是**真实可用**的。
    声明了自己做不到的能力，比不声明更糟 —— 上层会基于它做出错误决策。
    """

    #: 数据源类型标识，用于日志与诊断
    kind: ArtifactKind = ArtifactKind.UNKNOWN

    @property
    @abstractmethod
    def uri(self) -> str:
        """唯一位置标识。同一份数据重复构造应得到相同 uri。"""

    @abstractmethod
    def capabilities(self) -> frozenset[SourceCapability]:
        """本数据源实际具备的能力。"""

    def has(self, cap: SourceCapability) -> bool:
        return cap in self.capabilities()

    @abstractmethod
    def read_all(self) -> bytes:
        """全量读取。

        仅当内容确实需要整体处理时才调用。对 GB 级测量文件，
        应当使用 `stream()` 或 `read_at()`，否则内存会被打爆。
        """

    def stream(self, chunk_size: int = 1 << 20) -> Iterator[bytes]:
        """分块读取。默认实现退化到 read_all，可流式的实现应当覆盖它。"""
        data = self.read_all()
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def read_at(self, offset: int, size: int) -> bytes:
        """随机访问。未声明 SEEK 能力的实现会抛 NotSeekableError。"""
        if not self.has(SourceCapability.SEEK):
            raise NotSeekableError(f"{self.uri} 不支持随机访问")
        raise NotImplementedError

    def write_all(self, data: bytes) -> None:
        """全量写回。未声明 WRITE 能力的实现会抛 ReadOnlySourceError。"""
        if not self.has(SourceCapability.WRITE):
            raise ReadOnlySourceError(f"{self.uri} 是只读数据源")
        raise NotImplementedError

    @abstractmethod
    def artifact_ref(self) -> ArtifactRef:
        """构造供 Artifact 使用的位置引用（含内容哈希）。"""

    def describe(self) -> dict[str, Any]:
        """供日志与诊断用的自我描述。"""
        return {
            "kind": str(self.kind),
            "uri": self.uri,
            "capabilities": sorted(str(c) for c in self.capabilities()),
        }
