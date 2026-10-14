"""工件（Artifact）元模型 —— 统一骨架的第一根柱子。

设计要点：**工件不持有内容，只持有引用与指纹。**

这是刻意的。标定文件、测量文件动辄几十 MB 到几十 GB，
把它们复制进本系统的存储会带来三个后果：磁盘翻倍、版本歧义、
以及「到底哪个才是权威副本」的混乱。所以我们只记录
「它在哪、内容哈希是多少、它是干什么用的」，
真正的字节永远留在原始位置。

`role` 与 `kind` 必须分开，这一点很重要：
同一个 ELF 文件，在 CDM 场景里是「内存镜像」，在符号对齐场景里
又是「符号来源」。用 kind 表达格式，用 role 表达用途。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ArtifactKind",
    "ArtifactRole",
    "ArtifactRef",
    "Artifact",
    "new_artifact_id",
]


def new_artifact_id() -> str:
    return uuid.uuid4().hex[:16]


class ArtifactKind(StrEnum):
    """文件格式。新增格式时在这里登记，并配套一个 parser 插件。"""

    # 标定描述
    A2L = "a2l"
    # 内存镜像
    ELF = "elf"
    HEX = "hex"
    S19 = "s19"
    BIN = "bin"
    # 标定值集
    DCM = "dcm"
    CDFX = "cdfx"
    PARAMSET = "paramset"
    # 测量数据
    MDF4 = "mf4"
    BLF = "blf"
    ASC = "asc"
    TRC = "trc"
    PCAP = "pcap"
    # 模型
    SLX = "slx"
    M = "matlab-script"
    # 未知
    UNKNOWN = "unknown"


class ArtifactRole(StrEnum):
    """用途。决定这个工件会被哪一类消费方使用。"""

    DESCRIPTION = "description"
    """标定描述（A2L）：定义有哪些标定量、地址、转换方法、限值。"""

    IMAGE = "image"
    """内存镜像：地址空间 + 字节。CDM 与刷写共用。"""

    VALUES = "values"
    """值集：地址（或符号）到原始值的映射。"""

    SYMBOLS = "symbols"
    """符号来源：能提供 符号名 → 地址 的映射，用于跨版本对齐。"""

    MEASUREMENT = "measurement"
    """测量数据：通道 + 时间轴。"""

    MODEL = "model"
    """Simulink 模型或脚本。"""


class ArtifactRef(BaseModel):
    """对原始内容的位置引用。内容本身不进入本系统。"""

    model_config = ConfigDict(frozen=True)

    uri: str
    """本地路径或设备 URI。"""

    content_hash: str = ""
    """内容哈希。它是缓存命中与「文件是否变过」的唯一判据。
    空字符串表示尚未计算（例如在线数据源没有稳定内容）。"""

    size: int | None = None
    mtime: float | None = None
    """修改时间。仅用于快速短路判断，**不作为正确性依据**。"""


class Artifact(BaseModel):
    """本系统认知中的一个数据对象。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(default_factory=new_artifact_id)
    kind: ArtifactKind = ArtifactKind.UNKNOWN
    role: ArtifactRole = ArtifactRole.DESCRIPTION
    ref: ArtifactRef

    producer: str = ""
    """产出它的插件 id。用于溯源「这条数据是谁解析出来的」。"""

    label: str = ""
    """人类可读的显示名，例如数据集名或通道组名。"""

    meta: dict[str, Any] = Field(default_factory=dict)
    """插件自定义的附加信息。**约定：骨架不解释这里的内容**，
    因此插件可以自由扩展而不影响任何已有模块。"""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def cache_key(self, extra: str = "") -> str:
        """缓存键。基于内容哈希而非路径 —— 同一个文件换名字仍然命中缓存。"""
        base = self.ref.content_hash or f"{self.ref.uri}:{self.ref.mtime}"
        return f"{self.producer}:{self.kind}:{base}:{extra}"
