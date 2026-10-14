"""事件总线 —— 统一骨架的第三根柱子。

它解决一个具体问题：**长耗时操作要能持续汇报进度，又不能和界面耦合。**

刷写最典型：一条刷写序列可能跑十几分钟，界面必须实时看到进度、
不能卡死；而刷写的每一步逻辑又完全不该知道「谁会来订阅」。
事件总线就是这两者之间唯一的通道。

约定：事件的 topic 用点号分层（`job.progress`、`flash.step`），
订阅支持前缀匹配。新增一类事件**不需要**注册任何东西 ——
发布方只管发，没人订阅也不报错，这是解耦的关键。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Event", "EventBus", "Topics", "Unsubscribe"]


class Topics:
    """系统内置的 topic 约定。

    插件可以自由发明新的 topic，这里只登记骨架自身会发出的那些。
    """

    JOB_STARTED = "job.started"
    JOB_PROGRESS = "job.progress"
    JOB_FINISHED = "job.finished"

    SOURCE_OPENED = "source.opened"
    SOURCE_FAILED = "source.failed"

    LICENSE_STATE = "license.state"

    FLASH_STEP = "flash.step"
    FLASH_PROGRESS = "flash.progress"
    FLASH_ABORTED = "flash.aborted"


class Event(BaseModel):
    topic: str
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    job_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


Unsubscribe = Callable[[], None]
_Handler = Callable[[Event], None]


class EventBus:
    """进程内同步事件总线。

    刻意做得很薄：不做持久化、不做跨进程投递。
    需要跨进程时，由基础设施层把事件桥接出去，
    而不是把复杂度塞进骨架。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[_Handler]] = defaultdict(list)

    def subscribe(self, topic_prefix: str, handler: _Handler) -> Unsubscribe:
        """按 topic 前缀订阅，返回取消订阅的可调用对象。"""
        self._handlers[topic_prefix].append(handler)

        def _unsubscribe() -> None:
            bucket = self._handlers.get(topic_prefix)
            if not bucket:
                return
            try:
                bucket.remove(handler)
            except ValueError:
                pass
            if not bucket:
                self._handlers.pop(topic_prefix, None)

        return _unsubscribe

    def publish(self, event: Event) -> None:
        """同步分发。某个订阅者抛异常不会影响其它订阅者。"""
        for prefix, handlers in tuple(self._handlers.items()):
            if not event.topic.startswith(prefix):
                continue
            for handler in tuple(handlers):
                try:
                    handler(event)
                except Exception:  # noqa: BLE001 - 订阅者故障必须被隔离
                    # 订阅者是界面的、第三方插件的、日志的……
                    # 它们任何一个崩掉都不能让发布方（可能是正在刷写的 ECU）失败。
                    continue

    def clear(self) -> None:
        self._handlers.clear()
