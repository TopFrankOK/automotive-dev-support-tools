"""示例刷写配方 —— 把语义步骤翻译成请求字节。

**这里是协议专有知识唯一该待的地方。**

领域核心只认识「进入编程会话」「擦除」「传输」这类语义，
它不该、也不能知道这些步骤在总线上长什么样。
一旦把协议细节写进核心，「换个 ECU / 换个 Bootloader」
就从「加一个插件」退化成「改主程序」。

本配方使用一套**精简的诊断帧**编码（服务号与通用诊断规范一致，
但只实现到足以驱动与验证流程的程度），不是某个具体协议的完整实现。
真机上联调时，换掉这个插件即可，编排层与领域模型一字不用改。

## 两条刻意的严格

1. **认不出来的响应一律算失败，并保留原始字节。**
   把看不懂的响应当成成功，等于让一次实际上失败的刷写通过验收。
2. **只有「等一等再来」类错误才标可重试。**
   密钥错误重试一百次还是错的，还会把 ECU 锁死。
"""

from __future__ import annotations

import struct
from typing import Any

from automotive_dev_support.core.models.flash import (
    FlashRecipe,
    FlashStep,
    FlashStepKind,
    FlashStepOutcome,
)

__all__ = ["SimRecipe"]

#: 各步骤的正响应服务号（请求号 + 0x40）。
_POSITIVE: dict[FlashStepKind, frozenset[int]] = {
    FlashStepKind.ENTER_SESSION: frozenset({0x50}),
    FlashStepKind.SECURITY_ACCESS: frozenset({0x67}),
    FlashStepKind.ERASE: frozenset({0x71}),
    # 传输既有「请求下载」（0x34→0x74）也有「传输数据」（0x36→0x76）
    FlashStepKind.TRANSFER: frozenset({0x74, 0x76}),
    FlashStepKind.VERIFY: frozenset({0x71}),
    FlashStepKind.RESET: frozenset({0x51}),
}

#: 否定响应码 → 人能读的原因。抱着手册查 0x7F 0xXX 不该是使用者的活。
_NRC_TEXT: dict[int, str] = {
    0x11: "目标不支持该服务",
    0x12: "目标不支持该子功能",
    0x13: "报文长度或格式错误",
    0x22: "当前条件不满足（多半是还没进编程会话）",
    0x24: "请求顺序不对（例如还没请求种子就发了密钥）",
    0x31: "请求超出范围（地址或长度不合法）",
    0x33: "安全访问未通过（可能还没请求种子）",
    0x35: "密钥错误",
    0x36: "安全访问尝试次数超限（目标已锁定）",
    0x37: "要求的延时还没到",
    0x72: "一般编程失败（擦除或写入没成功）",
    0x73: "块序号不对",
    0x78: "目标正忙，请稍后重试",
    0x7E: "当前会话不支持该子功能",
    0x7F: "当前会话不支持该服务",
}

#: 值得重试的否定响应码。判据只有一个：**再等等就会好**的才算。
_RETRYABLE_NRC = frozenset({0x37, 0x73, 0x78})


class SimRecipe:
    """示例配方。实现 `core.ports.plugin.Recipe` 协议。"""

    recipe_id = "flash-recipe-sim"

    def describe(self) -> FlashRecipe:
        """给出本配方的完整时序。

        擦除与传输**不可取消**：中途停手会把 ECU 留在半擦除 / 半写入状态，
        那比多等一分钟糟得多。
        """
        return FlashRecipe(
            id=self.recipe_id,
            name="示例刷写配方（仿真）",
            version="0.1.0",
            preconditions={
                "security_level": 1,
                "max_image_bytes": 8 * 1024 * 1024,
            },
            steps=(
                FlashStep(
                    kind=FlashStepKind.ENTER_SESSION,
                    label="进入编程会话",
                    timeout_s=2.0,
                    max_retries=1,
                ),
                FlashStep(
                    kind=FlashStepKind.SECURITY_ACCESS,
                    label="安全访问",
                    timeout_s=2.0,
                ),
                FlashStep(
                    kind=FlashStepKind.ERASE,
                    label="擦除目标区域",
                    timeout_s=30.0,
                    cancellable=False,
                ),
                FlashStep(
                    kind=FlashStepKind.TRANSFER,
                    label="按段传输",
                    timeout_s=5.0,
                    max_retries=2,
                    cancellable=False,
                ),
                FlashStep(
                    kind=FlashStepKind.VERIFY,
                    label="校验",
                    timeout_s=10.0,
                    max_retries=1,
                ),
                FlashStep(
                    kind=FlashStepKind.RESET,
                    label="复位并确认存活",
                    timeout_s=5.0,
                ),
            ),
        )

    # -- 编码 ---------------------------------------------------------------

    def encode(self, step: Any, ctx: dict[str, Any]) -> bytes:
        kind = FlashStepKind(step.kind)
        ctx = ctx or {}

        if kind is FlashStepKind.ENTER_SESSION:
            return b"\x10\x02"

        if kind is FlashStepKind.SECURITY_ACCESS:
            seed = bytes(ctx.get("seed", b""))
            if not seed:
                return b"\x27\x01"
            # 密钥算法各家私有，属于本配方的知识，不放进编排层
            return b"\x27\x02" + bytes(b ^ 0x5A for b in seed)

        if kind is FlashStepKind.ERASE:
            start = int(ctx.get("start", 0))
            size = int(ctx.get("size", 0))
            return b"\x31\x01\xff\x00" + struct.pack(">II", start, size)

        if kind is FlashStepKind.TRANSFER:
            chunk = bytes(ctx.get("chunk", b""))
            if not chunk:
                # 请求下载：告知起始地址与长度
                start = int(ctx.get("start", 0))
                size = int(ctx.get("size", 0))
                return b"\x34\x00\x44" + struct.pack(">II", start, size)
            seq = int(ctx.get("seq", 1)) & 0xFF
            return b"\x36" + bytes([seq]) + chunk

        if kind is FlashStepKind.VERIFY:
            # 把镜像指纹带进请求，让校验有据可依
            fingerprint = str(ctx.get("image_fingerprint", ""))
            return b"\x31\x01\x02\x02" + fingerprint.encode("ascii")

        if kind is FlashStepKind.RESET:
            return b"\x11\x01"

        raise ValueError(f"未知的步骤类型：{kind!r}")

    # -- 解释 ---------------------------------------------------------------

    def interpret(self, step: Any, response: bytes) -> FlashStepOutcome:
        kind = FlashStepKind(step.kind)
        resp = bytes(response or b"")

        if not resp:
            return FlashStepOutcome(
                ok=False,
                kind=kind,
                detail="空响应（超时或链路已断）",
                retryable=True,
                response=resp,
            )

        if resp[0] == 0x7F:
            nrc = resp[2] if len(resp) > 2 else 0x00
            return FlashStepOutcome(
                ok=False,
                kind=kind,
                detail=_NRC_TEXT.get(nrc, f"未知否定响应码 0x{nrc:02X}"),
                retryable=nrc in _RETRYABLE_NRC,
                response=resp,
            )

        expected = _POSITIVE.get(kind, frozenset())
        if expected and resp[0] not in expected:
            return FlashStepOutcome(
                ok=False,
                kind=kind,
                detail=(
                    f"响应 0x{resp[0]:02X} 与请求不匹配"
                    f"（期望 0x{sorted(expected)[0]:02X}，已保留原始字节）"
                ),
                retryable=False,
                response=resp,
            )

        if kind is FlashStepKind.SECURITY_ACCESS and resp[:2] == b"\x67\x01":
            # 种子由本插件指出（响应体的哪几个字节是种子属于协议知识），
            # 编排层只管把它原样传回来。
            return FlashStepOutcome(
                ok=True,
                kind=kind,
                detail="已取得种子",
                response=resp,
                data=resp[2:],
            )

        return FlashStepOutcome(ok=True, kind=kind, detail="成功", response=resp)
