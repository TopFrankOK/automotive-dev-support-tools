"""领域核心。

**依赖规则（由 .importlinter 与 tests/test_layering.py 双重执行）：**
    - 不得 import apps / services / platform / plugins
    - 第三方依赖白名单只有 pydantic（纯数据层）与 cryptography（纯密码学原语）

子包：
    skeleton   统一骨架：Artifact / DataSource / EventBus / Job
    ports      冻结的扩展点契约与插件注册表
    models     分领域模型，各自独立演进
    licensing  授权纯逻辑：编解码 / 签发 / 校验

修改本包需要格外谨慎 —— 它是所有扩展点的地基。
具体业务字段请放到 `models` 下对应领域，不要往 `skeleton` 里加。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
