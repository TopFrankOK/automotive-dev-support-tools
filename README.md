# Automotive Software Development Support Tools

面向**汽车嵌入式软件开发**的桌面办公助手。目标是一个能长期、高频迭代的工具箱：
新增功能、修改某个功能、改 UI，彼此之间的交叉影响尽可能小。

## 当前状态

**P0（骨架 + 授权体系）已完成，P1（值集加载）已完成**，其余功能域尚未开始。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 分层骨架、插件注册表、离线授权体系 | ✅ 完成 |
| P1a | 值集加载（CDFX）与值—描述绑定 | ✅ 完成 |
| P1b | CDM 双面模型、HEX / DCM 解析与写回 | ⬜ 未开始 |
| P2 | 持久化：工件索引、内容哈希缓存、作业调度 | ⬜ 未开始 |
| P3 | 前端：工作台、差异表格、Map/Curve 视图 | ⬜ 未开始 |
| P4 | 数据分析：MF4/BLF 索引、降采样、趋势视图 | ⬜ 未开始 |
| P5 | 软件刷写：CAN/DoIP 网关、配方、审计日志 | ⬜ 未开始 |
| P6 | MATLAB 桥 + 独立 MATLAB 工程 | ⬜ 未开始 |

## 四个功能域

这四块业务的**执行模式本质不同**，因此架构上没有强行统一它们的数据模型，
只统一了「有输入、会花时间、要汇报进度」这部分骨架。

| 域 | 执行模式 | 硬要求 |
| --- | --- | --- |
| 标定数据 CDM | 离线 · 可重算 · 可回放 | 必须无损往返写回 |
| 数据分析 | 大文件 · 流式 · 只读 | 分块索引 + 降采样 |
| 模型工程助手 | 进程外 · 跨语言 · 启动慢 | 契约解耦，只负责启动 |
| 软件刷写 | 在线 · 有状态 · 不可重放 | 断点续刷 + 审计日志 |

## 架构

依赖方向只能向下，由 `import-linter` 与 AST 扫描在 CI 里强制执行，不靠自觉：

```
apps  ──▶  services  ──▶  platform  ──▶  core  ◀──  plugins
```

- `core/` 领域核心，零第三方依赖（pydantic 除外）
- `platform/` 基础设施：存储、缓存、作业、事件、配置、授权
- `plugins/` 一功能一文件夹，插件之间禁止互相 import
- `services/` 应用层用例与 FastAPI
- `apps/` 桌面壳（Tauri）与 CLI

完整设计见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 值集与描述是分开的

A2L 只说「有哪些量、长什么样」，值集（CDFX / DCM）只说「当前值是多少」。
两者分开演进，所以「把值贴到描述上」是一次**对齐**，不是赋值：

```python
a2l  = A2lParser().parse(FileSource("demo.a2l"), ctx).document      # 结构
vals = CdfxParser().parse(FileSource("demo.cdfx"), ctx).document    # 值
bound, binding = bind_values(a2l, vals)
print(binding.coverage, binding.missing_in_model, binding.missing_in_values)
```

对不上的部分**一律报告，绝不静默丢弃** ——
「贴完值了但某个量其实还是旧的」不会报错，只会让某次试验的结论悄悄错掉。

## 快速开始

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 跑测试
pytest tests/ -q

# 跑依赖方向契约（必须先设 PYTHONPATH）
PYTHONPATH=src lint-imports
```

## 授权

软件分发采用 **Ed25519 签名的文本许可码**：同事报机器码 → 作者用签发工具生成许可码
→ 一条微信发过去 → 粘贴激活。作者私钥离线保管，软件内只硬编码公钥。

```bash
python tools/license-issuer/issuer.py init     # 生成密钥对（只做一次）
python tools/license-issuer/issuer.py new --code <机器码> --name <姓名> --days 30
python tools/license-issuer/issuer.py renew --code <机器码>   # 续期，不损失剩余天数
```

**注意**：`tools/license-issuer/keys/` 下的私钥与签发台账已被 `.gitignore` 排除，
切勿提交。

## 许可

[MIT](LICENSE) — 但请注意，本仓库**不包含**作者私钥；任何人克隆本仓库都无法签发许可码。
