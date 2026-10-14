"""插件注册表。

职责很窄：**扫描 → 校验 → 导入 → 建索引**。

三条设计纪律：

1. **故障隔离。** 一个插件坏了（清单写错、导入失败、缺少依赖），
   只能导致它自己被标记为不可用，绝不能影响启动或其它插件。
   注册表把错误收集成 `LoadedPlugin.error`，由界面决定怎么提示。

2. **只读清单，不读代码。** 建索引完全依赖 `manifest.toml`，
   不需要导入插件本体。因此「列出有哪些功能」是零风险的，
   即便某个插件的实现依赖了没装的库。

3. **版本校验前置。** 契约版本不匹配的插件直接拒绝加载，
   而不是让它带着错误的假设跑进生产。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from automotive_dev_support.core.ports.plugin import API_VERSION, PluginManifest
from automotive_dev_support.core.skeleton.artifact import ArtifactKind
from automotive_dev_support.core.skeleton.capability import PluginCapability

__all__ = ["LoadedPlugin", "PluginRegistry", "MANIFEST_NAME"]

MANIFEST_NAME = "manifest.toml"


@dataclass
class LoadedPlugin:
    """一个已被认知的插件。`error` 非空表示它不可用。"""

    manifest: PluginManifest
    directory: Path
    instance: object | None = None
    error: str | None = None
    _extras: dict[str, object] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        state = "ok" if self.usable else f"error={self.error}"
        return f"<LoadedPlugin {self.manifest.id} {state}>"


class PluginRegistry:
    def __init__(self, api_version: str = API_VERSION) -> None:
        self._api_version = api_version
        self._plugins: dict[str, LoadedPlugin] = {}

    # -- 发现与加载 --------------------------------------------------------

    def discover(self, roots: Iterable[Path]) -> list[Path]:
        """扫描插件根目录，返回所有清单文件路径。不做任何导入。"""
        found: list[Path] = []
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                manifest = child / MANIFEST_NAME
                if child.is_dir() and manifest.is_file():
                    found.append(manifest)
        return found

    def load_all(self, roots: Iterable[Path]) -> None:
        for manifest_path in self.discover(roots):
            self._load_one(manifest_path)

    def _load_one(self, manifest_path: Path) -> None:
        try:
            manifest = PluginManifest.from_toml(manifest_path)
        except Exception as exc:  # noqa: BLE001 - 清单坏了也只是这一个插件坏
            # 清单都读不出来时连 id 都没有，用目录名兜底以便用户定位
            fallback = PluginManifest(id=manifest_path.parent.name, entry="_:x")
            self._plugins[fallback.id] = LoadedPlugin(
                manifest=fallback,
                directory=manifest_path.parent,
                error=f"清单解析失败：{exc}",
            )
            return

        plugin = LoadedPlugin(manifest=manifest, directory=manifest_path.parent)

        if manifest.api_version != self._api_version:
            plugin.error = (
                f"契约版本不匹配：插件声明 {manifest.api_version}，"
                f"内核为 {self._api_version}"
            )
            self._plugins[manifest.id] = plugin
            return

        try:
            plugin.instance = self._import_entry(manifest.entry)
        except Exception as exc:  # noqa: BLE001
            plugin.error = f"导入失败：{exc}"

        self._plugins[manifest.id] = plugin

    @staticmethod
    def _import_entry(entry: str) -> object:
        module_name, _, attr = entry.partition(":")
        module = importlib.import_module(module_name)
        target = getattr(module, attr, None)
        if target is None:
            raise AttributeError(f"{module_name} 里没有 {attr}")
        # 支持「类」与「返回实例的工厂函数」两种写法
        return target() if callable(target) else target

    # -- 索引 ---------------------------------------------------------------

    def __iter__(self) -> Iterator[LoadedPlugin]:
        return iter(self._plugins.values())

    def __len__(self) -> int:
        return len(self._plugins)

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return self._plugins.get(plugin_id)

    def by_capability(self, capability: PluginCapability) -> list[LoadedPlugin]:
        return [
            p
            for p in self._plugins.values()
            if p.usable
            and p.manifest.enabled
            and capability in p.manifest.capabilities
        ]

    def parsers_for(self, kind: ArtifactKind) -> list[LoadedPlugin]:
        return [
            p
            for p in self.by_capability(PluginCapability.PARSER)
            if kind in p.manifest.handles
        ]

    def blocked_by_license(self, granted: Iterable[str]) -> list[LoadedPlugin]:
        """列出因授权不足而不可用的插件。供界面提示「此功能需要授权」。"""
        granted_set = set(granted)
        return [
            p
            for p in self._plugins.values()
            if p.usable
            and any(req not in granted_set for req in p.manifest.requires)
        ]

    def usable(self) -> list[LoadedPlugin]:
        return [p for p in self._plugins.values() if p.usable]

    def broken(self) -> list[LoadedPlugin]:
        return [p for p in self._plugins.values() if not p.usable]

    def summary(self) -> dict[str, object]:
        return {
            "total": len(self._plugins),
            "usable": len(self.usable()),
            "broken": [
                {"id": p.manifest.id, "error": p.error} for p in self.broken()
            ],
            "capabilities": {
                str(cap): [p.manifest.id for p in self.by_capability(cap)]
                for cap in PluginCapability
            },
        }
