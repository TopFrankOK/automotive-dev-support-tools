"""刷写流程领域模型 —— 定义「一次刷写由什么组成」。

这一块是**纯数据 + 纯函数**，不碰硬件、不碰进程、不碰界面。
理由很直接：刷写不可重放，标定比对错了可以重跑，刷写错了是物理事故。
凡是能在没有 ECU 的机器上想清楚的语义，就应该在没有 ECU 的机器上想清楚。

## 与标定域的关系

刷写与标定共用 `models.memory.AddressSpace`（「把镜像按段写进 ECU」
与「把值写进镜像的对应地址」处理的是同一个东西）。
除此之外两域互不感知：本模块不认识标定量，也不认识任何具体协议。

## 三条刻意的严格

1. **配方指纹包含步骤顺序。** 配方名没变、步骤还是那几步，
   但「先擦后传」改成「先传后擦」是致命的 —— 而名字完全看不出来。
2. **检查点只装游标与指纹，不装数据。** 装了镜像字节，
   状态文件会跟着膨胀到几十 MB，而且下次续刷读到的到底是哪个副本就说不清了。
3. **续刷校验要指明是哪一项不一致。** 只回一句「不能续刷」，
   使用者就得自己猜是换了文件、换了配方，还是换了个 ECU。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from automotive_dev_support.core.models.common import now_utc, sha256_hex

__all__ = [
    "FlashRegion",
    "FlashTarget",
    "FlashStepKind",
    "FlashStep",
    "FlashRecipe",
    "FlashProgress",
    "FlashCheckpoint",
    "FlashRecord",
    "FlashStepOutcome",
    "canonical_bytes",
]


def canonical_bytes(payload: Any) -> bytes:
    """指纹计算的规范字节。

    `sort_keys=True` 是必须的：否则「同样的内容、不同的字段书写顺序」
    会算出两个指纹，表现为「明明是我的配方却说换了配方」。
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def fingerprint_of(payload: Any) -> str:
    return sha256_hex(canonical_bytes(payload))


class FlashStepKind(StrEnum):
    """一个刷写步骤的**语义**类型。

    刻意使用语义名而不是协议术语：本协议无关的命名，
    是把「具体协议怎么编码」留给插件的前提。
    """

    ENTER_SESSION = "enter_session"
    """进入编程会话。刷写的前提状态。"""

    SECURITY_ACCESS = "security_access"
    """安全访问（种子—密钥交换）。"""

    ERASE = "erase"
    """擦除目标区域。"""

    TRANSFER = "transfer"
    """传输数据（按段下载）。"""

    VERIFY = "verify"
    """校验（校验和 / 回读比对）。"""

    RESET = "reset"
    """复位并确认目标存活。"""


class FlashStep(BaseModel):
    """一步。

    不可变（`frozen`）：改一步就等于换了一个配方，
    必须重新算指纹，而不是就地改一下了事。
    """

    model_config = ConfigDict(frozen=True)

    kind: FlashStepKind
    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    """步骤参数。语义由配方插件解释，核心不认识它。"""

    timeout_s: float = 5.0
    """单次尝试的超时。"""

    max_retries: int = 0
    """失败重试次数。默认 0 —— 出问题时先停下拉报错，不要默默重试。"""

    cancellable: bool = True
    """本步执行中是否允许取消。擦除与传输中必须设为 False。"""

    def as_fingerprint_part(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "label": self.label,
            "params": self.params,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "cancellable": self.cancellable,
        }


class FlashRecipe(BaseModel):
    """刷写配方：有序步骤 + 前置条件。

    适配一个新 ECU = 加一个配方，**不改主程序**。
    这是本域最重要的一条可替换性。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    version: str = "0.1.0"
    preconditions: dict[str, Any] = Field(default_factory=dict)
    """前置条件（例如镜像体积上限、要求的安全等级）。语义由配方插件解释。"""

    steps: tuple[FlashStep, ...] = ()
    """有序步骤。**顺序本身是配方的一部分**，改顺序即换配方。"""

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(
            {
                "id": self.id,
                "version": self.version,
                "name": self.name,
                "preconditions": self.preconditions,
                "steps": [s.as_fingerprint_part() for s in self.steps],
            }
        )


class FlashRegion(BaseModel):
    """目标上的一段可寻址区域。"""

    model_config = ConfigDict(frozen=True)

    start: int
    size: int
    label: str = ""
    writable: bool = True

    @property
    def end(self) -> int:
        """末地址（不含）。"""
        return self.start + self.size


class FlashTarget(BaseModel):
    """刷写目标：一台具体的 ECU。

    `channel` 与 `diagnostic_id` 是**交给网关的连接与寻址参数**，
    本模型不解释它们的含义 —— 该怎么连、怎么寻址是插件的知识。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    channel: str = ""
    """网关通道描述，原样传给网关的连接参数。"""

    diagnostic_id: str = ""
    """诊断标识（逻辑寻址用）。"""

    regions: tuple[FlashRegion, ...] = ()
    """目标上的可写区域布局。"""

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(
            {
                "id": self.id,
                "name": self.name,
                "diagnostic_id": self.diagnostic_id,
                "regions": [
                    {
                        "start": r.start,
                        "size": r.size,
                        "label": r.label,
                        "writable": r.writable,
                    }
                    for r in self.regions
                ],
            }
        )


class FlashProgress(BaseModel):
    """进度：当前第几步、已完成几段、已传多少字节。"""

    recipe_id: str = ""
    step_index: int = -1
    """当前步骤下标。-1 表示还没开始。"""

    step_kind: FlashStepKind | None = None
    segments_done: int = 0
    segments_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""

    @property
    def ratio(self) -> float | None:
        """完成比例。两者都不知道时返回 None（而不是假装 0）。

        越界会被夹住：进度条倒退比不显示更糟。
        """
        if self.bytes_total > 0:
            raw = self.bytes_done / self.bytes_total
        elif self.segments_total > 0:
            raw = self.segments_done / self.segments_total
        else:
            return None
        return max(0.0, min(1.0, raw))


class FlashCheckpoint(BaseModel):
    """续刷检查点：完成游标 + 三项指纹。

    刻意只装游标与指纹，不装镜像数据。
    """

    job_id: str = ""
    recipe_id: str = ""
    recipe_fingerprint: str = ""
    image_fingerprint: str = ""
    target_fingerprint: str = ""

    segment_index: int = 0
    """已完成到第几段（游标 = 已完成的段数）。"""

    bytes_done: int = 0
    erased: bool = False
    """擦除是否已做过。续刷时不该重复擦。"""

    updated_at: datetime = Field(default_factory=now_utc)

    def mismatches(
        self,
        recipe_fingerprint: str,
        image_fingerprint: str,
        target_fingerprint: str,
    ) -> list[str]:
        """返回不一致的项名称；空列表表示三项都对得上，可以续刷。

        逐项比对而不是整体比对，是为了在拒绝续刷时能说清**哪一项**变了。
        """
        bad: list[str] = []
        if recipe_fingerprint != self.recipe_fingerprint:
            bad.append("recipe")
        if image_fingerprint != self.image_fingerprint:
            bad.append("image")
        if target_fingerprint != self.target_fingerprint:
            bad.append("target")
        return bad


class FlashStepOutcome(BaseModel):
    """一步的执行结果。

    **失败必须带人能读的原因**，不能只回一句「失败了」。
    认不出来的响应要保留原始字节 —— 把它当成成功，
    等于让一次实际上失败的刷写通过验收。
    """

    ok: bool = False
    kind: FlashStepKind | None = None
    detail: str = ""
    """人能读的原因。失败时必填。"""

    retryable: bool = False
    """是否值得重试。超时与「时序未到」值得；密钥错误不值得。"""

    response: bytes = b""
    """原始响应字节。认不出来时靠它兜底，绝不丢弃。"""

    data: bytes = b""
    """解释出来的有效载荷（例如安全访问取到的种子）。

    由配方插件填写 —— 「响应里哪几个字节是种子」是协议知识，
    编排层不该知道，否则协议细节就又漏回上层了。
    """

    @field_serializer("response", "data")
    def _ser_bytes(self, value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")


class FlashRecord(BaseModel):
    """审计记录：一次刷写作业的完整留痕。

    成功与失败都要记 —— 失败的留痕正是
    「这台 ECU 是不是被反复刷失败过」这个问题的唯一答案。
    """

    job_id: str
    started_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None
    duration_s: float = 0.0

    operator: str = ""
    target_id: str = ""
    target_name: str = ""
    image_fingerprint: str = ""
    recipe_id: str = ""
    recipe_fingerprint: str = ""

    success: bool = False
    error: str = ""
    failed_step_index: int | None = None
    failed_step_kind: FlashStepKind | None = None

    segments_done: int = 0
    segments_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0

    def finish(
        self,
        success: bool,
        at: datetime | None = None,
        error: str = "",
        failed_step_index: int | None = None,
        failed_step_kind: FlashStepKind | None = None,
    ) -> None:
        """收尾。未指定时间时用当前 UTC 时间。"""
        self.finished_at = at or now_utc()
        self.duration_s = max(
            0.0, (self.finished_at - self.started_at).total_seconds()
        )
        self.success = success
        self.error = error
        self.failed_step_index = failed_step_index
        self.failed_step_kind = failed_step_kind
