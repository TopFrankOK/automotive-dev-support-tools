"""刷写审计记录。

「谁在什么时间给哪台 ECU 刷了哪个版本的什么文件」——
这个问题在体系审核时一定会被问到，所以这份记录必须长期留存，
不能被日志轮转清掉，也不能因为加了一个字段就让历史读不出来。

## 两个刻意的选择

1. **一行一条 JSON，不用带表头的表格。**
   表格（CSV 之类）一旦要在中间加字段，历史文件就得做表头迁移；
   漏迁移一列，整行数据会**静默错位一列** —— 那是最难发现的一类错误。
2. **读的时候遇到坏行必须报错，不能跳过。**
   静默跳过一条记录，等于这份「谁刷过什么」的账本悄悄少了一页。
"""

from __future__ import annotations

import os
from pathlib import Path

from automotive_dev_support.core.models.flash import FlashRecord

__all__ = ["AuditLog"]


class AuditLog:
    """追加式审计日志。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: FlashRecord) -> None:
        """追加一条。整行一次性落盘，所以中断不会留下半条记录。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = record.model_dump_json()
        if "\n" in line:  # 理论上不会，真出现了就是序列化器坏了
            raise ValueError("审计记录序列化后竟含换行，拒绝写入")
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def all(self) -> list[FlashRecord]:
        records: list[FlashRecord] = []
        for number, line in self._read_lines():
            try:
                records.append(FlashRecord.model_validate_json(line))
            except Exception as exc:
                raise ValueError(
                    f"审计日志第 {number} 行无法解析（{self._path}）：{exc}"
                ) from exc
        return records

    def by_target(self, target_id: str) -> list[FlashRecord]:
        """这台 ECU 被刷过什么。"""
        return [r for r in self.all() if r.target_id == target_id]

    def by_image(self, image_fingerprint: str) -> list[FlashRecord]:
        """这个版本的镜像刷过哪些台。"""
        return [
            r for r in self.all() if r.image_fingerprint == image_fingerprint
        ]

    def _read_lines(self) -> list[tuple[int, str]]:
        if not self._path.exists():
            return []
        out: list[tuple[int, str]] = []
        for number, line in enumerate(
            self._path.read_text("utf-8").splitlines(), start=1
        ):
            if line.strip():
                out.append((number, line))
        return out
