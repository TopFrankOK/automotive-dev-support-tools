"""统一骨架 —— 定义「数据怎么被处理」，不定义「数据是什么」。

四根柱子：
    Artifact    工件元模型（不持有内容，只持有引用与指纹）
    DataSource  数据源抽象（所有数据的来处归一到这里）
    EventBus    事件总线（长耗时操作汇报进度而不耦合界面）
    Job         作业执行模型（四种执行模式跑在同一条跑道上）

**修改本包需要格外谨慎。** 它是所有扩展点的地基，
它一变，所有插件都要跟着变。领域数据请放到 `core.models` 下，
并且按领域分目录 —— 不要往这里塞任何具体业务字段。
"""

from automotive_dev_support.core.skeleton.artifact import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ArtifactRole,
    new_artifact_id,
)
from automotive_dev_support.core.skeleton.capability import PluginCapability, SourceCapability
from automotive_dev_support.core.skeleton.datasource import (
    DataSource,
    NotSeekableError,
    ReadOnlySourceError,
    SourceError,
)
from automotive_dev_support.core.skeleton.event import Event, EventBus, Topics
from automotive_dev_support.core.skeleton.job import (
    CancelledError,
    Job,
    JobContext,
    JobResult,
    JobSpec,
    JobState,
    Progress,
)

__all__ = [
    # artifact
    "Artifact",
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactRole",
    "new_artifact_id",
    # capability
    "PluginCapability",
    "SourceCapability",
    # datasource
    "DataSource",
    "SourceError",
    "ReadOnlySourceError",
    "NotSeekableError",
    # event
    "Event",
    "EventBus",
    "Topics",
    # job
    "Job",
    "JobContext",
    "JobResult",
    "JobSpec",
    "JobState",
    "Progress",
    "CancelledError",
]
