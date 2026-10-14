"""作业（Job）执行模型 —— 统一骨架的第四根柱子。

这是把四种本质不同的执行模式装进同一个跑道的那个抽象：

    CDM 比对      → 离线、可重算、可取消
    测量降采样    → 流式、CPU 密集、可取消
    刷写          → 在线、不可重放、**可续**、不可随便取消
    MATLAB 调用   → 进程外、启动慢、不可取消（只能等待）

它们的共同点只有三件事：**有输入、会花时间、要汇报进度**。
Job 就只定义这三件事。剩下的差异全部交给 `JobSpec` 的参数
与 `JobState` 的状态迁移，而不是在骨架里写 if/elif。

关键设计：`resumable`。
刷写到 80% 断了，重跑一遍是不可接受的（可能是十几分钟，
而且会反复擦写 ECU 的 Flash）。所以「可续」必须是一等公民，
由 Job 自己声明，由调度器据此决定失败后的行为。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from automotive_dev_support.core.skeleton.artifact import Artifact
from automotive_dev_support.core.skeleton.event import Event, EventBus, Topics

__all__ = [
    "JobState",
    "JobSpec",
    "Progress",
    "JobResult",
    "JobContext",
    "Job",
    "CancelledError",
]


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancelledError(Exception):
    """由 JobContext.raise_if_cancelled() 抛出，调度器据此转入 CANCELLED。"""


class JobSpec(BaseModel):
    """一次作业的完整输入描述。可序列化 —— 因此能落库、能重放、能排队。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str
    """作业类型。由插件自定义，骨架不解释。"""

    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)

    requires: list[str] = Field(default_factory=list)
    """执行本作业所需的授权功能项（对应许可码的 features）。
     授权闸门在派发作业前统一校验，Job 自身不必关心授权。"""

    resumable: bool = False
    """失败后是否支持续跑。刷写必须为 True。"""

    cancellable: bool = True
    """是否允许用户中途取消。刷写的关键步骤应设为 False。"""

    timeout_s: float | None = None
    id: str = Field(default_factory=lambda: uuid4().hex[:12])


class Progress(BaseModel):
    done: int = 0
    total: int | None = None
    message: str = ""

    @property
    def ratio(self) -> float | None:
        if not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))


class JobResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: JobState
    outputs: list[Artifact] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    @classmethod
    def ok(cls, **kw: Any) -> JobResult:
        return cls(state=JobState.SUCCEEDED, **kw)

    @classmethod
    def failed(cls, error: str, **kw: Any) -> JobResult:
        return cls(state=JobState.FAILED, error=error, **kw)


class JobContext:
    """作业运行时上下文。Job 通过它汇报进度、检查取消、发布事件。

    这是 Job 与外界**唯一**的接触面 —— Job 拿不到界面、拿不到数据库，
    因此它天然可测、可重放、可跨进程执行。
    """

    def __init__(
        self,
        job_id: str,
        bus: EventBus | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.job_id = job_id
        self._bus = bus
        self._cancel_check = cancel_check
        self._last: Progress | None = None

    def report(self, done: int, total: int | None = None, message: str = "") -> None:
        """汇报进度。重复的相同进度不会重复发事件（避免刷爆总线）。"""
        progress = Progress(done=done, total=total, message=message)
        if self._last is not None and progress == self._last:
            return
        self._last = progress
        self._publish(
            Event(
                topic=Topics.JOB_PROGRESS,
                job_id=self.job_id,
                payload={"done": done, "total": total, "message": message},
            )
        )

    def emit(self, topic: str, **payload: Any) -> None:
        """发布自定义事件。插件可以自由发明新 topic。"""
        self._publish(Event(topic=topic, job_id=self.job_id, payload=payload))

    @property
    def cancelled(self) -> bool:
        return bool(self._cancel_check and self._cancel_check())

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError

    def _publish(self, event: Event) -> None:
        if self._bus is not None:
            self._bus.publish(event)


class Job(ABC):
    """可执行单元。插件通过实现它来提供能力。"""

    kind: ClassVar[str] = ""

    @abstractmethod
    def run(self, spec: JobSpec, ctx: JobContext) -> JobResult:
        """同步执行。调度器负责把它丢进工作进程，Job 自己不关心线程模型。"""

    def estimate_cost(self, spec: JobSpec) -> float:
        """粗略代价估计，供调度器排序。默认不可知。"""
        return 0.0


def now() -> datetime:
    return datetime.now(timezone.utc)
