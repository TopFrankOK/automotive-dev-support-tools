"""仿真网关 —— 不碰硬件的字节通道。

它存在的唯一理由：**让整套刷写流程在没有 CAN 卡、没有 ECU 的机器上
也能被完整驱动和验证。** 刷写逻辑的复杂度（时序、重试、断点续刷、审计）
与「字节怎么上总线」完全无关，把它们绑在一起测试只会又慢又脆，
而且逻辑错误会被硬件噪声掩盖。

## 三条刻意的严格

1. **脚本耗尽必须报错，绝不静默返回空字节。**
   静默返回空，上层很可能把它当成一次成功的响应 ——
   那正是本项目最忌讳的「不报错，只是安静地错」。
2. **失败的请求也记进请求日志。**
   否则「发出去了没回」和「根本没发」永远分不清。
3. **不解释连接参数的含义。**
   该怎么连是具体网关的知识，这里只原样收下。

脚本里可以放三种东西：
    bytes     —— 正常返回这一串字节
    TIMEOUT   —— 这次请求超时（抛 `TimeoutError`，链路仍然可用）
    DROP      —— 这次请求后链路断开（抛 `ConnectionError`，后续请求全被拒）

异常一律用内置类型（`TimeoutError` / `ConnectionError`），
这样上层捕获时不需要 import 本插件 —— 插件不该成为上层的依赖。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["SimGateway", "TIMEOUT", "DROP", "make_sim_gateway"]


class _Marker:
    """脚本里的一个动作标记（不是响应字节）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


TIMEOUT = _Marker("TIMEOUT")
DROP = _Marker("DROP")


class SimGateway:
    """脚本化的网关。实现 `core.ports.plugin.Gateway` 协议。"""

    channel = "sim"

    def __init__(
        self, script: Sequence[bytes | _Marker] = (), *, echo: bool = False
    ) -> None:
        """
        `echo=True` 时，脚本用尽后按「正响应 = 服务号 + 0x40」回环应答，
        用来把整条流程跑通（命令行演示用）。

        默认 `echo=False` 是刻意的：**脚本耗尽必须报错**，
        否则上层会把空响应当成成功。
        """
        self._script: list[bytes | _Marker] = list(script)
        self._cursor = 0
        self._connected = False
        self._requests: list[bytes] = []
        self._params: dict[str, Any] = {}
        self._echo = echo

    # -- 协议 ---------------------------------------------------------------

    def connect(self, params: dict[str, Any]) -> None:
        self._params = dict(params or {})
        self._connected = True
        self._cursor = 0
        self._requests = []

    def disconnect(self) -> None:
        self._connected = False

    def request(self, payload: bytes, timeout_s: float = 1.0) -> bytes:
        if not self._connected:
            raise ConnectionError("仿真网关未连接")

        # 先记后发：无论成败，「确实发出过什么」都必须可核对
        self._requests.append(bytes(payload))

        if self._cursor >= len(self._script):
            if self._echo:
                return _echo_positive(payload)
            raise RuntimeError(
                "仿真脚本已耗尽：第 "
                f"{self._cursor + 1} 次请求没有预设响应。"
                "（刻意抛错而不是返回空字节 —— 空字节会被当成成功响应。）"
            )

        item = self._script[self._cursor]
        self._cursor += 1

        if isinstance(item, _Marker):
            if item is TIMEOUT:
                raise TimeoutError(f"仿真超时（{timeout_s}s）")
            if item is DROP:
                self._connected = False
                raise ConnectionError("仿真链路断开")
            raise RuntimeError(f"未知的脚本标记：{item!r}")

        return bytes(item)

    @property
    def connected(self) -> bool:
        return self._connected

    # -- 观测（供测试核对，不属于协议） --------------------------------------

    @property
    def requests(self) -> list[bytes]:
        """按发送顺序记录的请求字节。"""
        return list(self._requests)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)


def _echo_positive(payload: bytes) -> bytes:
    """回环应答：按通用诊断规范，正响应服务号 = 请求服务号 + 0x40。

    安全访问是唯一需要「多给一点」的地方：请求种子时若只回一个空正响应，
    上层拿不到种子就会停在「取种子成功但没有拿到种子字节」——
    所以这里给一个固定种子，让整条流程能真的走完。

    只用于跑通流程，**不模拟任何真实时序**。
    """
    if not payload:
        return b""
    service = payload[0]
    sub = payload[1] if len(payload) > 1 else 0x00
    if service == 0x27 and sub == 0x01:  # 请求种子
        return b"\x67\x01\x11\x22"
    if service == 0x27:  # 发送密钥
        return b"\x67\x02"
    return bytes([service + 0x40, sub])


def make_sim_gateway() -> SimGateway:
    """注册表用的入口：一个回环模式的仿真网关。

    命令行与演示走这里 —— 不写脚本也能把整套流程跑一遍。
    需要精确复现超时、否定响应、断连时，直接构造 `SimGateway(script=...)`
    （那种模式是严格的：脚本耗尽即报错）。
    """
    return SimGateway(echo=True)
