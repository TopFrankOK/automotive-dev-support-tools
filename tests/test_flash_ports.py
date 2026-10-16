"""配方扩展点的测试。

协议是**内核与插件之间的合同**，所以这里测的不是行为，而是三件事：

1. 新协议确实存在，且能按结构校验（`runtime_checkable`）
2. **已有协议一个字都没动** —— 扩展点只增不改，
   动一下就是让所有既有插件一起失效
3. 已有插件在新增协议之后仍然全部加载成功

第 2、3 条是真正的重点：加新能力时把旧能力弄坏，
是最容易发生的那种「改 A 崩 B」。
"""

from __future__ import annotations

from typing import Any

from automotive_dev_support.core.ports.plugin import (
    Analyzer,
    Exporter,
    Gateway,
    Parser,
    Recipe,
    View,
)
from automotive_dev_support.core.ports.registry import PluginRegistry
from automotive_dev_support.core.skeleton.capability import PluginCapability
from conftest import PLUGINS_DIR


class _FullRecipe:
    recipe_id = "demo"

    def describe(self) -> Any:
        return None

    def encode(self, step: Any, ctx: dict[str, Any]) -> bytes:
        return b""

    def interpret(self, step: Any, response: bytes) -> Any:
        return None


class _MissingEncode:
    recipe_id = "demo"

    def describe(self) -> Any:
        return None

    def interpret(self, step: Any, response: bytes) -> Any:
        return None


class TestRecipeProtocol:
    def test_complete_implementation_satisfies_protocol(self):
        assert isinstance(_FullRecipe(), Recipe)

    def test_missing_method_does_not_satisfy_protocol(self):
        """缺一个方法就不算实现了协议。

        这条断言的意义：让「差一点点的配方插件」在结构校验时就被发现，
        而不是等到刷写到一半才发现某一步译不出字节。
        """
        assert not isinstance(_MissingEncode(), Recipe)

    def test_capability_enum_has_recipe(self):
        assert PluginCapability.RECIPE.value == "recipe"


class TestExistingProtocolsUntouched:
    def test_gateway_still_has_its_original_members(self):
        """网关协议必须原样保留 —— 已有插件正按它实现。"""
        for name in ("connect", "disconnect", "request", "connected"):
            assert hasattr(Gateway, name), name

    def test_all_previous_protocols_still_exported(self):
        for proto in (Parser, Analyzer, Exporter, View, Gateway, Recipe):
            assert hasattr(proto, "__protocol_attrs__") or hasattr(proto, "_is_protocol")


class TestExistingPluginsStillLoad:
    def test_registry_loads_every_plugin_without_breakage(self):
        registry = PluginRegistry()
        registry.load_all([PLUGINS_DIR])
        broken = registry.broken()
        assert not broken, "有插件加载失败：\n" + "\n".join(
            f"  {p.manifest.id}: {p.error}" for p in broken
        )
        assert len(list(registry)) >= 9
