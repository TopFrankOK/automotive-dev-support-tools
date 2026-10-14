"""基础设施层。

存放「有副作用」的东西：文件、注册表、进程、网络。
**领域逻辑不要放这里** —— 这里的所有模块都应当是
「薄」的，它们只把外部世界翻译成 `core` 认识的数据形状。

    store      元数据库（SQLite）与工件索引        [待建]
    cache      按内容哈希的解析结果缓存             [待建]
    jobs       作业调度与工作进程池                 [待建]
    events     事件总线到前端（WebSocket）的桥接    [待建]
    config     配置读写                             [待建]
    logging    日志与审计日志                       [待建]
    licensing  机器指纹 / 本地状态 / 授权服务       ✅

依赖规则：只允许依赖 `core`。
"""

from __future__ import annotations

__all__: list[str] = []
