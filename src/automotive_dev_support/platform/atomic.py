"""原子落盘的公用实现。

**为什么单独放一个模块**：需要原子替换的地方不止一处（数据源写回、
刷写检查点）。各写一遍就会出现「一处重试、一处不重试」这种不一致，
而它表现出来的症状是**偶发失败** —— 最难查的那一种，
重跑一遍又好了，于是永远没人真正去修。

**为什么需要重试**：Windows 上刚写完的 `.tmp` 文件会被杀毒软件的
实时扫描短暂占用，此时 `os.replace` 抛 `PermissionError`。
这不是本软件的缺陷，但后果很糟：新文件已经写好了却没换上去，
用户看到的是一句莫名其妙的「拒绝访问」，
而真正的语义只是「晚几十毫秒再来一次就行」。

只重试 `PermissionError`：其它 OSError（磁盘满、路径不存在）
重试多少次都不会变好，立刻抛出来才是对的。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["replace_atomically"]

_ATTEMPTS = 6
_DELAY_S = 0.02


def replace_atomically(tmp: Path, target: Path) -> None:
    """把 `tmp` 换到 `target` 上。

    这是原子操作：任何时刻磁盘上都是一个完整的文件，
    不会出现「旧版本没了、新版本还没写完」的半截状态 ——
    那种状态在标定写回里等于把用户的基线弄丢了。
    """
    last: OSError | None = None
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(tmp, str(target))
            return
        except PermissionError as exc:
            last = exc
            time.sleep(_DELAY_S * (attempt + 1))
    assert last is not None  # 循环至少执行一次，走到这里必有异常
    raise last
