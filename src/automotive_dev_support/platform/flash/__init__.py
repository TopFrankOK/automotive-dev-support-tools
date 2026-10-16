"""刷写域的基础设施 —— 有副作用的那部分（落盘）。

领域模型在 `core/models/flash`，编排在 `services/flash`；
这里只负责把状态与记录写进文件系统。

    CheckpointStore  续刷检查点（一个目标一个文件，原子替换）
    AuditLog         审计记录（一行一条，追加写入，长期留存）
"""

from automotive_dev_support.platform.flash.audit import AuditLog
from automotive_dev_support.platform.flash.checkpoint import CheckpointStore

__all__ = ["AuditLog", "CheckpointStore"]
