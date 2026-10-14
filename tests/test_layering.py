"""架构纪律的自动化执行。

`.importlinter` 管的是**模块之间**的依赖，但它管不了两件事：
  1. core 从**外部包**引入依赖（比如偷偷 `import openpyxl`）
  2. 具体的违规位置与原因，报错信息不够直白

所以这里用 AST 扫描补上。它比 `.importlinter` 更早、更安静地拦住问题 ——
而且报错信息能直接告诉你是哪个文件、哪一行不该出现什么。

**这类测试看起来像是给自己找麻烦，实际上是唯一能让架构活过三个月的东西。**
人一定会偷懒：「就先 import 一下，回头再整理」。三个月后回头整理的成本，
远高于当初被测试拦下来的那 30 秒。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conftest import PLUGINS_DIR, REPO_ROOT

SRC = REPO_ROOT / "src"
PKG = SRC / "automotive_dev_support"
CORE = PKG / "core"
PLUGINS = PKG / "plugins"

#: core 允许的第三方依赖白名单。
#:
#: 只有两个，而且都是**纯计算库**（无 IO、无框架、无网络）：
#:   pydantic     数据校验与序列化 —— 它同时是 contracts/ 里 schema 的来源
#:   cryptography 密码学原语 —— 等价于标准库的延伸
#:
#: 任何会引入 IO、框架或业务耦合的库都不该出现在这里。
#: 例如 openpyxl（导出）、cantools（总线）、fastapi（接口）都必须在 core 之外。
ALLOWED_THIRD_PARTY = {"pydantic", "cryptography"}

FORBIDDEN_PREFIXES = (
    "automotive_dev_support.services",
    "automotive_dev_support.platform",
    "automotive_dev_support.apps",
    "automotive_dev_support.plugins",
)


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _absolute_imports(path: Path) -> set[str]:
    """模块里出现的所有绝对导入的顶层模块名（相对导入会被跳过）。

    用 AST 而不是正则：注释和字符串里出现的 `import` 不该被误判。
    """
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 表示相对导入，天然安全
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


# ---------------------------------------------------------------------------
# 纪律 1：core 的第三方依赖白名单
# ---------------------------------------------------------------------------


def test_core_only_uses_allowed_third_party():
    violations: dict[str, list[str]] = {}

    for path in _iter_python_files(CORE):
        for module in _absolute_imports(path):
            top = module.split(".")[0]
            if top == "automotive_dev_support":
                continue
            if top in sys.stdlib_module_names:
                continue
            if top in ALLOWED_THIRD_PARTY:
                continue
            violations.setdefault(top, []).append(_rel(path))

    assert not violations, (
        "core 引入了白名单外的第三方依赖。\n"
        "领域核心必须保持纯净 —— 一旦引入 IO 或框架，"
        "所有外层改动都会穿透到地基。\n"
        "正确做法：把需要该库的逻辑放到 platform/ 或 plugins/ 下。\n\n"
        + "\n".join(
            f"  {module} ← " + "、".join(sorted(set(files)))
            for module, files in sorted(violations.items())
        )
    )


# ---------------------------------------------------------------------------
# 纪律 2：core 不得依赖外层
# ---------------------------------------------------------------------------


def test_core_does_not_depend_on_outer_layers():
    violations: list[str] = []

    for path in _iter_python_files(CORE):
        for module in _absolute_imports(path):
            if module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"  {_rel(path)} → {module}")

    assert not violations, (
        "core 依赖了外层模块，依赖方向被反转了。\n"
        "契约是 apps → services → platform → core ← plugins，\n"
        "core 必须在最底层，不依赖任何人。\n\n"
        "正确做法：如果 core 需要某个能力，在 core/ports 里定义协议，\n"
        "由 platform 或 plugins 实现它（依赖倒置）。\n\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 纪律 3：插件只依赖 core
# ---------------------------------------------------------------------------


def test_plugins_only_depend_on_core():
    violations: list[str] = []

    for path in _iter_python_files(PLUGINS):
        for module in _absolute_imports(path):
            if module.startswith(
                ("automotive_dev_support.services", "automotive_dev_support.platform", "automotive_dev_support.apps")
            ):
                violations.append(f"  {_rel(path)} → {module}")

    assert not violations, (
        "插件依赖了 core 之外的内核模块，会失去「可独立开发、可抛弃」的性质。\n\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 纪律 4：插件之间互不依赖
# ---------------------------------------------------------------------------


def test_plugins_do_not_import_each_other():
    plugin_names = sorted(
        d.name
        for d in PLUGINS.iterdir()
        if d.is_dir() and (d / "manifest.toml").is_file()
    )
    assert plugin_names, "没有发现任何插件，插件目录结构可能被改坏了"

    violations: list[str] = []
    for path in _iter_python_files(PLUGINS):
        owner = path.relative_to(PLUGINS).parts[0]
        for module in _absolute_imports(path):
            if not module.startswith("automotive_dev_support.plugins."):
                continue
            target = module.split(".")[2]
            if target != owner:
                violations.append(f"  {_rel(path)}（属于 {owner}）→ {module}")

    assert not violations, (
        "插件之间出现了互相 import —— 这会重新引入「改 A 崩 B」。\n"
        "需要共享类型时，把类型上提到 core 里。\n"
        "参考 core/models/calibration/diff.py 中 ChangeSet 的位置说明。\n\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 纪律 5：扩展点契约版本一致
# ---------------------------------------------------------------------------


def test_manifests_declare_current_api_version():
    """插件声明的契约版本必须与内核一致。

    版本不一致的插件会被注册表拒绝加载 —— 这是刻意的，
    好过让它带着错误的假设跑进生产。
    """
    import tomllib

    from automotive_dev_support.core.ports.plugin import API_VERSION

    mismatches: list[str] = []
    for manifest in sorted(PLUGINS.glob("*/manifest.toml")):
        data = tomllib.loads(manifest.read_text("utf-8")).get("plugin", {})
        declared = data.get("api_version")
        if declared != API_VERSION:
            mismatches.append(
                f"  {_rel(manifest)} 声明 {declared!r}，内核为 {API_VERSION!r}"
            )

    assert not mismatches, "契约版本不一致：\n" + "\n".join(mismatches)


def test_plugin_entry_points_are_importable():
    """清单里声明的入口必须真的能被解析出来。

    把这条做成测试，是为了避免「界面上显示有这个功能，点进去报错」
    这种最让人困惑的失败。
    """
    import importlib

    from automotive_dev_support.core.ports.registry import PluginRegistry

    registry = PluginRegistry()
    registry.load_all([PLUGINS])

    broken = registry.broken()
    assert not broken, "有插件加载失败：\n" + "\n".join(
        f"  {p.manifest.id}: {p.error}" for p in broken
    )

    for plugin in registry:
        module_name, _, attr = plugin.manifest.entry.partition(":")
        module = importlib.import_module(module_name)
        assert hasattr(module, attr), f"{plugin.manifest.id} 的入口 {attr} 不存在"


def test_plugins_directory_is_discoverable():
    registry_root = PLUGINS_DIR
    assert registry_root.is_dir()
    manifests = sorted(registry_root.glob("*/manifest.toml"))
    assert len(manifests) >= 2, "至少应有 parser-a2l 与 diff-calibration 两个示例插件"
