"""仿真网关的测试。

这个插件存在的唯一理由：**让刷写流程在没有 ECU 的机器上也能被完整验证。**
所以它自己必须绝对可靠 —— 一个会悄悄返回空响应的仿真网关，
会让上层所有「刷写成功」的结论都变成假的。

重点断言：

  - 脚本按序号返回，顺序不能错
  - 脚本耗尽必须**报错**，不能静默返回空字节
    （静默返回空 = 上层可能把空响应当成成功，这正是本项目最忌讳的那种错）
  - 未连接时发请求必须被拒（而不是假装发出去了）
  - 收到的每一次请求都可被逐字节核对
"""

from __future__ import annotations

import pytest

from automotive_dev_support.core.ports.plugin import Gateway
from automotive_dev_support.plugins.gateway_sim import (
    DROP,
    TIMEOUT,
    SimGateway,
)


def test_satisfies_gateway_protocol():
    assert isinstance(SimGateway(), Gateway)


class TestScriptedResponses:
    def test_returns_responses_in_order(self):
        gw = SimGateway(script=[b"\x50\x01", b"\x71\x01\x02"])
        gw.connect({})
        assert gw.request(b"\x10\x02") == b"\x50\x01"
        assert gw.request(b"\x27\x01") == b"\x71\x01\x02"

    def test_script_exhaustion_raises_instead_of_returning_empty(self):
        """静默返回空字节是最坏的结果：上层可能把它当成「成功」。"""
        gw = SimGateway(script=[b"\x50\x01"])
        gw.connect({})
        assert gw.request(b"\x10\x02") == b"\x50\x01"
        with pytest.raises(RuntimeError):
            gw.request(b"\x10\x03")

    def test_timeout_is_raised(self):
        gw = SimGateway(script=[TIMEOUT, b"\x50\x01"])
        gw.connect({})
        with pytest.raises(TimeoutError):
            gw.request(b"\x10\x02")
        # 超时之后仿真链路仍然可用，后续脚本继续生效
        assert gw.request(b"\x10\x02") == b"\x50\x01"

    def test_drop_disconnects_and_further_requests_are_refused(self):
        gw = SimGateway(script=[DROP, b"\x50\x01"])
        gw.connect({})
        with pytest.raises(ConnectionError):
            gw.request(b"\x10\x02")
        assert gw.connected is False
        with pytest.raises(ConnectionError):
            gw.request(b"\x10\x02")

    def test_empty_script_refuses_everything(self):
        gw = SimGateway()
        gw.connect({})
        with pytest.raises(RuntimeError):
            gw.request(b"\x10\x02")


class TestConnectionState:
    def test_requests_before_connect_are_refused(self):
        gw = SimGateway(script=[b"\x50\x01"])
        assert gw.connected is False
        with pytest.raises(ConnectionError):
            gw.request(b"\x10\x02")

    def test_disconnect_then_reconnect_resets_script(self):
        gw = SimGateway(script=[b"\x50\x01", b"\x71\x01"])
        gw.connect({})
        gw.request(b"\x10\x02")
        gw.disconnect()
        gw.connect({})
        assert gw.request(b"\x10\x02") == b"\x50\x01"

    def test_connect_records_params_without_interpreting_them(self):
        """连接参数原样收下 —— 该怎么连是具体网关的知识，仿真网关不解释。"""
        gw = SimGateway()
        gw.connect({"target": "sim://ecu-1"})
        assert gw.connected is True


class TestRequestLog:
    def test_every_request_is_recorded_verbatim(self):
        gw = SimGateway(script=[b"\x50", b"\x71", b"\x76"])
        gw.connect({})
        gw.request(b"\x10\x02")
        gw.request(b"\x27\x01")
        gw.request(b"\x31\x01")
        assert tuple(gw.requests) == (b"\x10\x02", b"\x27\x01", b"\x31\x01")

    def test_failed_requests_are_recorded_too(self):
        """失败的请求也要留痕 —— 否则「发出去了没回」和「根本没发」分不清。"""
        gw = SimGateway(script=[TIMEOUT])
        gw.connect({})
        with pytest.raises(TimeoutError):
            gw.request(b"\x10\x02")
        assert tuple(gw.requests) == (b"\x10\x02",)

    def test_reconnect_clears_the_log(self):
        gw = SimGateway(script=[b"\x50"])
        gw.connect({})
        gw.request(b"\x10\x02")
        gw.connect({})
        assert tuple(gw.requests) == ()


class TestManifest:
    def test_is_discovered_as_a_gateway_plugin(self):
        from automotive_dev_support.core.ports.registry import PluginRegistry
        from conftest import PLUGINS_DIR

        registry = PluginRegistry()
        registry.load_all([PLUGINS_DIR])
        plugin = next(
            (p for p in registry if p.manifest.id == "gateway-sim"), None
        )
        assert plugin is not None, "注册表没有发现仿真网关插件"
        assert plugin.error is None, f"插件加载失败：{plugin.error}"
        assert "gateway" in [c.value for c in plugin.manifest.capabilities]
