"""Automotive Software Development Support Tools —— 汽车嵌入式研发办公助手。

分层（依赖只能向下）：
    apps  →  services  →  platform  →  core  ←  plugins

    core       领域核心：统一骨架 + 扩展点契约 + 分领域模型 + 授权逻辑
    platform   基础设施：存储 / 缓存 / 作业 / 事件 / 配置 / 指纹 / 授权状态
    services   应用层：用例编排
    apps       交互层：CLI（桌面壳在仓库根的 apps/desktop）
    plugins    可插拔功能，只依赖 core，且互相独立

设计基线见 ARCHITECTURE.md。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
