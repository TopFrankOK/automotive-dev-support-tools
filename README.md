# Automotive Software Development Support Tools

面向**汽车嵌入式软件开发**的桌面办公助手。目标是一个能长期、高频迭代的工具箱：
新增功能、修改某个功能、改 UI，彼此之间的交叉影响尽可能小。

## 当前状态

**P0（骨架 + 授权体系）、P1（标定 CDM 端到端）、P2（测量数据分析）、P3（软件刷写）均已完成**，
其余功能域尚未开始。
每个阶段在 git 上都有对应 tag：`p0` 骨架与授权，`p1` 标定端到端，`p2` 测量数据分析，`p3` 软件刷写。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 分层骨架、插件注册表、离线授权体系 | ✅ 完成 |
| P1 | 标定 CDM 端到端：A2L / CDFX / HEX / S19 解析、值加载与绑定、比对、Excel 报告、CLI | ✅ 完成 |
| P2 | 数据分析：MF4 / BLF / ASC 索引、对齐与降采样、趋势命令 | ✅ 完成 |
| P3 | 软件刷写：配方扩展点、仿真网关、断点续刷、审计留痕、CLI | ✅ 完成 |
| P4 | CDM 无损写回：值→字节、校验和真算、Writer 扩展点、HEX 写回、`ads writeback` | ✅ 完成（S19 待补） |
| P5 | MATLAB 桥 + Tauri 桌面壳 + FastAPI 服务层 | ⬜ 未开始 |

> P3 落地的是**可验证的刷写逻辑**：配方、断点续刷、审计、取消保护都在
> 「字节边界」上跑通（仿真网关驱动，无硬件依赖）。
> 真实总线（CAN / DoIP）与独立进程网关**不在本期** —— 接口已按可序列化设计好，
> 加 `gateway-can` / `gateway-doip` 插件即可，编排层与领域模型不用改。

### 已内置插件

| 插件 | 能力 | 说明 |
| --- | --- | --- |
| `parser-a2l` | parser | A2L 描述文件 → `CalibrationModel`（符号路径索引） |
| `parser-cdfx` | parser | CDFX 值集 → `ValueSet`（树形实例路径拼接） |
| `parser-hex` | parser | Intel HEX → `AddressSpace`（逐行 checksum，坏记录不加载） |
| `parser-s19` | parser | Motorola S-Record → `AddressSpace` |
| `parser-asc` | parser | Vector ASC 日志 → 事件流（统计全量、帧体有界） |
| `parser-blf` | parser | Vector BLF 二进制日志 → 事件流（对象头逐类校验） |
| `parser-mf4` | parser | MDF4 块链索引 → 连续通道组（懒加载）+ 总线事件组 |
| `diff-calibration` | analyzer | 两个标定模型 → `ChangeSet`（地址漂移单独归类） |
| `export-excel-diff` | exporter | `ChangeSet` → Excel 报告（汇总 / 明细 / 地址漂移） |
| `gateway-sim` | gateway | 仿真网关：脚本化字节通道，可复现超时 / 否定响应 / 断连 |
| `flash-recipe-sim` | recipe | 示例刷写配方：语义步骤 → 请求字节，失败翻译成可读原因 |

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
# 安装开发依赖（导出 Excel 报告需加 excel  extra）
pip install -e ".[dev,excel]"

# 跑测试
pytest tests/ -q

# 跑依赖方向契约（必须先设 PYTHONPATH）
PYTHONPATH=src lint-imports
```

### 命令行：标定比对

```bash
# 值集版（CDFX）
ads compare --baseline-a2l old.a2l --baseline-values old.cdfx \
            --candidate-a2l new.a2l --candidate-values new.cdfx \
            --out report.xlsx

# 内存镜像版（HEX / S19）—— 值按 A2L 描述的地址与类型从镜像读出
ads compare --baseline-a2l old.a2l --baseline-image old.hex \
            --candidate-a2l new.a2l --candidate-image new.s19 \
            --out report.xlsx

# 看看有哪些插件可用
ads plugins
```

### 命令行：测量数据

```bash
# 一个测量文件里有什么（连续通道组 + 总线报文 ID）
ads signals run.mf4

# 抽稀后的趋势（min/max 频带，尖峰不因抽样丢失）
ads trend run.mf4 --signal EngineSpeed --points 40
ads trend run.mf4 --signal EngineSpeed --from 0.2 --to 0.5
ads trend bus.asc --signal ch1/0x850          # 事件流：报文速率

# 两次测量的同一条信号对齐到公共栅格，报告差异
ads align old.mf4 new.mf4 --signal EngineSpeed --step 0.01
```

**MF4 是懒加载的**：`ads signals` 只建索引不读数据，GB 级文件也是毫秒级；
取值发生在 `trend` / `align` 真正需要那一条信号时。

### 命令行：软件刷写

```bash
# 把镜像刷进目标（存在可续的检查点时自动从中断处继续）
ads flash run app.hex --target ECM-01 --operator 张三

# 从上次中断处继续（没有可续的检查点会报错，退出码 2）
ads flash resume app.hex --target ECM-01

# 历史刷写记录（成功与失败都留痕），可按目标或镜像指纹过滤
ads flash history --target ECM-01
```

### 命令行：CDM 写回

```bash
# 把值集写回内存镜像（原地写回，原子替换）
ads writeback app.hex --a2l app.a2l --values new.cdfx

# 写到副本，原文件一个字节都不动
ads writeback app.hex --a2l app.a2l --values new.cdfx --out patched.hex
```

**只改被改动的字节**：未改动的字节逐字节搬运、行尾沿用原文件，
所以 diff 里不会出现「整文件都变了」这种没法评审的结果。
没有配校验和规则时会明确提示 —— 改了值不更新校验和，刷进 ECU 会被拒。

退出码：成功 `0` / 刷写失败 `1` / 状态不对（镜像不存在、没得续、拒绝续刷）`2`。

**续刷会先校验三项指纹**（配方 / 镜像 / 目标），任何一项不一致就**拒绝执行**并说清是哪一项 ——
从半截接着刷出一个「既不是旧版也不是新版」的 ECU，是这类系统里后果最严重的静默错误。
擦除与传输步骤**不可取消**：中途停手会把目标留在半擦除状态。

状态与审计记录放在应用数据目录（可用 `ADS_FLASH_STATE_DIR` 覆盖）。

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
