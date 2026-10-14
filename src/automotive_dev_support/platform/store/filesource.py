"""本地文件数据源。

放在 `platform` 而不是 `core` 的原因：它做 IO。
放在 `platform` 而不是 `plugins` 的原因：几乎所有解析器都要用它，
属于基础设施而非可选功能。

**这个位置本身就是一个架构说明：**
解析器插件永远不需要 import 本模块 —— 它们只接收一个 `DataSource`
实例。所以将来把数据源换成版本库、网络盘、或某个厂商的设备通道时，
所有解析器一行都不用改。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

from automotive_dev_support.core.skeleton.artifact import ArtifactKind, ArtifactRef
from automotive_dev_support.core.skeleton.capability import SourceCapability
from automotive_dev_support.core.skeleton.datasource import DataSource, SourceError

__all__ = ["FileSource", "kind_from_suffix"]

_SUFFIX_KINDS: dict[str, ArtifactKind] = {
    ".a2l": ArtifactKind.A2L,
    ".elf": ArtifactKind.ELF,
    ".axf": ArtifactKind.ELF,
    ".out": ArtifactKind.ELF,
    ".hex": ArtifactKind.HEX,
    ".ihx": ArtifactKind.HEX,
    ".s19": ArtifactKind.S19,
    ".srec": ArtifactKind.S19,
    ".mot": ArtifactKind.S19,
    ".bin": ArtifactKind.BIN,
    ".dcm": ArtifactKind.DCM,
    ".cdfx": ArtifactKind.CDFX,
    ".cdf": ArtifactKind.CDFX,
    ".pa": ArtifactKind.PARAMSET,
    ".mf4": ArtifactKind.MDF4,
    ".mdf": ArtifactKind.MDF4,
    ".dat": ArtifactKind.MDF4,
    ".blf": ArtifactKind.BLF,
    ".asc": ArtifactKind.ASC,
    ".trc": ArtifactKind.TRC,
    ".pcap": ArtifactKind.PCAP,
    ".pcapng": ArtifactKind.PCAP,
    ".slx": ArtifactKind.SLX,
    ".m": ArtifactKind.M,
}

_CHUNK = 1 << 20


def kind_from_suffix(path: str | Path) -> ArtifactKind:
    return _SUFFIX_KINDS.get(Path(path).suffix.lower(), ArtifactKind.UNKNOWN)


class FileSource(DataSource):
    def __init__(self, path: str | Path, writable: bool = True) -> None:
        self._path = Path(path)
        if not self._path.exists() and writable:
            raise SourceError(f"文件不存在：{self._path}")

    @property
    def uri(self) -> str:
        return str(self._path.resolve())

    @property
    def path(self) -> Path:
        return self._path

    @property
    def kind(self) -> ArtifactKind:
        return kind_from_suffix(self._path)

    def capabilities(self) -> frozenset[SourceCapability]:
        caps = {
            SourceCapability.READ,
            SourceCapability.STREAM,
            SourceCapability.SEEK,
        }
        if self._path.exists() and os.access(self._path, os.W_OK):
            caps.add(SourceCapability.WRITE)
        return frozenset(caps)

    # -- 读 ----------------------------------------------------------------

    def read_all(self) -> bytes:
        try:
            return self._path.read_bytes()
        except OSError as exc:
            raise SourceError(f"读取失败：{self._path}（{exc}）") from exc

    def stream(self, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        """分块读取。大文件必须走这条路，否则内存会被打爆。"""
        try:
            with self._path.open("rb") as fh:
                while chunk := fh.read(chunk_size):
                    yield chunk
        except OSError as exc:
            raise SourceError(f"读取失败：{self._path}（{exc}）") from exc

    def read_at(self, offset: int, size: int) -> bytes:
        if not self.has(SourceCapability.SEEK):
            return super().read_at(offset, size)
        try:
            with self._path.open("rb") as fh:
                fh.seek(offset)
                return fh.read(size)
        except OSError as exc:
            raise SourceError(f"随机读取失败：{self._path}（{exc}）") from exc

    # -- 写 ----------------------------------------------------------------

    def write_all(self, data: bytes) -> None:
        """写入。**先写临时文件再原子替换** —— 这是必须的。

        标定写回最怕的就是「写到一半崩了」：原文件被截断，
        既没有新版本也没有旧版本，用户手上的基线直接没了。
        原子替换保证任何时刻磁盘上都是一个完整文件。
        """
        if not self.has(SourceCapability.WRITE):
            return super().write_all(data)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, self._path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise SourceError(f"写入失败：{self._path}（{exc}）") from exc

    # -- 元信息 ------------------------------------------------------------

    def artifact_ref(self) -> ArtifactRef:
        stat = self._path.stat() if self._path.exists() else None
        return ArtifactRef(
            uri=self.uri,
            content_hash=self.content_hash(),
            size=stat.st_size if stat else None,
            mtime=stat.st_mtime if stat else None,
        )

    def content_hash(self) -> str:
        """流式计算 SHA-256。GB 级文件也不会爆内存。"""
        digest = hashlib.sha256()
        try:
            with self._path.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()
