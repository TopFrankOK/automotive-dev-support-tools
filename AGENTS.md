# AGENTS.md

面向**汽车嵌入式软件开发**的桌面工具箱（标定数据 CDM / 数据分析 / 软件刷写 / 模型工程助手）。
Python 实现，当前唯一入口是 CLI `ads`。

## 先读这两篇

| 文件 | 讲什么 | 什么时候读 |
|---|---|---|
| `HANDOFF.md` | 现在做到哪、怎么跑起来、踩过的坑 | **每次开工前先读** |
| `ARCHITECTURE.md` | 为什么这样设计 | 改架构前必读；改了架构要同步改它 |

## 硬性纪律（由工具强制，不是建议）

1. 依赖方向只能向下：`apps → services → platform → core ← plugins`
2. `core/` 内禁止出现具体格式名（`a2l` / `hex` / `s19` / `cdfx` / `mf4` / `blf` / `asc` / `uds`），出现即抽象泄漏
3. 插件之间禁止互相 import；需要共享的类型上提到 `core/`
4. `core/` 的第三方依赖白名单只有 `pydantic` 与 `cryptography`
5. 扩展点**只增不改**；新增插件要在 `.importlinter` 契约 3 里登记

## 跑起来（PowerShell，不要用 Git Bash）

```powershell
Set-Location <仓库根>
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "$PWD\src;C:\Users\赵林\.workbuddy\binaries\python\envs\default\Lib\site-packages"
$py = "C:\Users\赵林\.workbuddy\binaries\python\versions\3.13.12\python.exe"

& $py -m pytest tests --tb=short -p no:cacheprovider        # 预期 352 passed
& $py -m automotive_dev_support.apps.cli.main plugins       # 预期 9 个插件可用
& $py -c "import sys; sys.argv=['lint-imports']; from importlinter.cli import lint_imports; lint_imports()"
```

四个必须记住的细节：

- **不要用 venv 里的 `python.exe`**，它是坏的（`sys.prefix` 解析错，表现为 `No module named pytest`）。
- **不要用 Git Bash 跑 Python**，它会把 `PYTHONPATH` 的两个条目绞成一个字符串。
- **`PYTHONUTF8=1` 必须设**，否则 `lint-imports` 在中文 Windows 上直接崩（`gbk codec`）。
- **`openpyxl` 没装时测试静默缩到 `222 passed, 2 skipped`**（`test_cli.py` 与 `test_export.py` 整文件 `importorskip`）。跑完请核对末尾那一行是不是真的 352。

## Agent skills

### Issue tracker

票据以本地 markdown 存在 `.scratch/<feature-slug>/` 下，不走 GitHub Issues。See `docs/agents/issue-tracker.md`.

### Triage labels

使用五个默认角色标签（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`），标签串与角色名同名。See `docs/agents/triage-labels.md`.

### Domain docs

single-context：仓库根一份 `GLOSSARY.md` + `docs/adr/`。See `docs/agents/domain.md`.
