"""续刷检查点的持久化。

检查点是「刷到一半」这件事的唯一证据，所以它有两个必须守住的性質：

1. **写入必须原子。** 写到一半崩了，要么是新版本要么是旧版本 ——
   绝不能留下一个半截的检查点，让人误以为可以从那儿接着刷。
2. **读不出来必须报错，不能当成「没有检查点」。**
   当成没有，会让人从头再刷一遍，白等十几分钟还白白擦写一次目标。
"""

from __future__ import annotations

import os
from pathlib import Path

from automotive_dev_support.core.models.flash import FlashCheckpoint

__all__ = ["CheckpointStore"]


class CheckpointStore:
    """一个目标一个检查点文件的读写。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> FlashCheckpoint | None:
        """读取检查点。文件不存在返回 None；存在但读不懂则报错。"""
        if not self._path.exists():
            return None
        raw = self._path.read_text("utf-8")
        try:
            return FlashCheckpoint.model_validate_json(raw)
        except Exception as exc:  # 损坏必须被看见，不能静默当没有
            raise ValueError(
                f"检查点文件损坏，无法据此续刷（{self._path}）：{exc}"
            ) from exc

    def save(self, checkpoint: FlashCheckpoint) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(checkpoint.model_dump_json(), encoding="utf-8")
        os.replace(tmp, self._path)

    def clear(self) -> None:
        """清除检查点。

        刷写成功后必须清掉 —— 留一个陈旧检查点，
        下次一旦指纹碰巧对上，就会被拿来「续」一次其实早就完成的刷写。
        """
        if self._path.exists():
            self._path.unlink()
