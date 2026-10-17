# 交接给 AI 助手的工作指南

> **这份文档的用途**：让任何一个 AI 编程助手（CodeBuddy / WorkBuddy / 其他）在读完它之后，
> 能在 10 分钟内把项目跑起来，知道现在做到哪、下一步该干什么、以及**哪些事情绝对不能做**。
>
> 与 `ARCHITECTURE.md` 的分工：
> `ARCHITECTURE.md` 讲**为什么这样设计**（设计意图，静态）；
> 本文讲**现在什么状态、怎么动手、哪里有坑**（工作上下文，动态）。
> 两者都要读，先读本文。

---

## 0. 三十秒版本

一个给**汽车嵌入式软件开发工程师**用的桌面工具箱，长期高频迭代。
核心设计目标是：**新增功能、改某个功能、改 UI，彼此交叉影响尽量小**。

四个功能域，执行模式本质不同，所以**没有强行统一数据模型**：

| 域 | 执行模式 | 硬要求 | 状态 |
|---|---|---|---|
| 标定数据 CDM | 离线 · 可重算 · 可回放 | 必须无损往返写回 | ✅ P1 完成 + P4 写回（HEX + S19） |
| 数据分析 | 大文件 · 流式 · 只读 | 分块索引 + 降采样 | ✅ P2 完成 |
| 模型工程助手 | 进程外 · 跨语言 · 启动慢 | 契约解耦，只负责启动 | ⬜ P4 |
| 软件刷写 | 在线 · 有状态 · 不可重放 | 断点续刷 + 审计日志 | ✅ P3 完成（**真实总线待接**） |

**已跑通的部分**：全部测试通过、11 个插件全部可用、依赖契约 4 kept / 0 broken、
CLI `compare` / `signals` / `trend` / `align` / `flash` / `plugins` 可冒烟。
**桌面 GUI 还没做**（Tauri 在 P4），目前唯一入口是 CLI `ads`。

> **P3 交付的是刷写逻辑，不是总线驱动。**
> 配方、断点续刷、审计、取消保护全部在**仿真网关**上跑通（无硬件依赖）。
> 真实 CAN / DoIP 与独立进程网关不在本期：网关接口只传字节，
> 届时加 `gateway-can` / `gateway-doip` 插件即可，编排层与领域模型一行都不用改。

---

## 1. 怎么把项目跑起来（AI 最容易卡死的地方，先读这节）

### 1.1 环境位置

```
Python 底座 : C:\Users\赵林\.workbuddy\binaries\python\versions\3.13.12\python.exe
venv        : C:\Users\赵林\.workbuddy\binaries\python\envs\default\
项目根      : <本仓库根>
```

⚠️ **venv 里的 `python.exe` 是坏的**，不要用它。它的 `sys.prefix` 解析成 `...\envs`
而不是 `...\envs\default`，导致 `import pytest` 直接 `ModuleNotFoundError`——
**极易误判成"依赖没装"**，实际 site-packages 里什么都装着。

**正确跑法：用底座 python + PYTHONPATH 指到 venv 的 site-packages。**

### 1.2 跑测试（PowerShell，不要用 Git Bash）

```powershell
Set-Location <项目根>
$env:PYTHONPATH = "$PWD\src;C:\Users\赵林\.workbuddy\binaries\python\envs\default\Lib\site-packages"
& "C:\Users\赵林\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m pytest tests --tb=short -p no:cacheprovider
```

预期：`473 passed in ~5s`（P4 之后；P3 是 352，P2 是 250）。

**三个必须记住的命令行细节：**

1. **不要用 Git Bash 跑 Python。** Git Bash 会把 `PYTHONPATH` 的两个条目绞成一个字符串
   （分隔符语义和盘符全丢），表现就是 `No module named pytest`。
2. **`pyproject.toml` 已配 `pythonpath = ["src"]`**，所以 pytest 不需要再手动设 `src`。
3. **`addopts = "-q"` 已在 pyproject 里，命令行别再写 `-q`。**
   写成 `-q -q` 会静默吞掉 `N passed` 汇总行，让你以为测试没跑。

### 1.3 跑依赖方向契约

```powershell
# python -m importlinter 不工作（没有 __main__），必须这样调：
& "C:\Users\赵林\.workbuddy\binaries\python\versions\3.13.12\python.exe" -c `
  "import sys; sys.argv=['lint-imports']; from importlinter.cli import lint_imports; lint_imports()"
```

预期：`Contracts: 4 kept, 0 broken`（Analyzed 68 files, 123 dependencies）。

### 1.4 CLI 冒烟

```powershell
& "...\python.exe" -m automotive_dev_support.apps.cli.main plugins
# 预期：共发现 11 个插件，可用 11 个
```

`ads` 入口命令：`compare` / `signals` / `trend` / `align` / `flash` / `plugins`。
CLI 刻意保持薄，逻辑全在 `services/`，桌面壳（Tauri）将来走同一条路。

刷写命令冒烟（走仿真网关，不需要硬件）：

```powershell
& "...\python.exe" -m automotive_dev_support.apps.cli.main flash run tests/data/sample_v1.hex --target ECM-01
& "...\python.exe" -m automotive_dev_support.apps.cli.main flash history
```

### 1.5 安装依赖

`typer`、`openpyxl` 等已声明在 `pyproject.toml`。
**注意**：曾经出现过「依赖已声明但环境没装」导致测试无法收集的情况
（`typer` 是 CLI 入口，属于核心依赖不是可选功能）。
换环境时第一件事就是 `pip install -e ".[dev,excel]"` 然后跑测试验证。

---

## 2. 代码地图

```
src/automotive_dev_support/
├── core/           ★ 领域核心，零第三方依赖（pydantic 除外）
│   ├── skeleton/      统一骨架：capability / artifact / datasource / event / job
│   ├── ports/         冻结扩展点：API_VERSION="1" + PluginRegistry（坏插件隔离）
│   ├── licensing/     授权纯逻辑（五道关卡），不碰硬件
│   └── models/        分领域模型，各自独立演进，互不感知
│       ├── common.py      SymbolPath —— 稳定逻辑标识，address 只是会漂移的属性
│       ├── memory/        AddressSpace + ChecksumSpec + ImageAssembler
│       ├── calibration/   双面模型（描述/值分开）+ diff
│       ├── measurement/   model + decode + decimate + align
│       ├── flash/         （刷写域，P3 尚未填充）
│       └── signal/
├── platform/       基础设施（**当前实装只有** `store/` 与 `licensing/`（指纹+防篡改）；
│                    cache/jobs/events/config/logging 尚未实装，别按设计文档去找）
├── services/       应用层用例：compare.py / analyze.py / measurement.py
├── plugins/        ★ 一功能一文件夹（注意：已在包内，不在仓库根）
└── apps/cli/       Typer CLI 入口

contracts/          ir/capability schema + openapi（★ 慎改）
apps/desktop/       Tauri 壳（P4，目前只有占位）
tools/license-issuer/  许可码签发 CLI，不打包进发布版
tests/data/         样例数据（sample_v1/v2 的 a2l/cdfx/hex/s19/mf4/asc/blf）
```

⚠️ **插件位置**：插件已经**从仓库根 `plugins/` 搬进了 `src/automotive_dev_support/plugins/`**。
仓库根现在只有 `apps/ contracts/ docs/ src/ tests/ tools/`。
新增插件要放**包内**，`importlib` 扫描路径随之改变。

**当前 9 个插件**：`parser_a2l` / `parser_cdfx` / `parser_hex` / `parser_s19` /
`parser_asc` / `parser_blf` / `parser_mf4`（以上 parser），
`diff_calibration`（analyzer），`export_excel_diff`（exporter）。

---

## 3. 不要推翻的架构决策

### 3.1 最重要的一条：没有统一 IR

早期假设「三条业务本质相同，可以统一成一个 IR」**已被推翻**。
现在是**统一骨架 + 分领域模型**两层：

- **统一骨架**（极稳定，扩展点地基）：DataSource / Job / EventBus / Artifact
- **分领域模型**（各自独立演进，互不感知）：标定 / 内存镜像 / 信号时序 / 刷写流程

**绝对不要把领域数据塞进一个统一 IR**——会变成上帝对象，直接破坏迭代独立性，
而这个独立性正是整个项目存在的理由。

### 3.2 值集与描述是分开的

A2L 只说「有哪些量、长什么样」，值集（CDFX）只说「当前值是多少」。
两者分开演进，所以「把值贴到描述上」是一次**对齐，不是赋值**：

```python
bound, binding = bind_values(a2l, valueset)
print(binding.coverage, binding.missing_in_model, binding.missing_in_values)
```

对不上的部分**一律报告，绝不静默丢弃**。
理由：「贴完值了但某个量其实还是旧的」不会报错，只会让某次试验的结论悄悄错掉。

### 3.3 双面模型（CDM 的核心难点）

CDM 的「读」已就绪（parse），「写」还没做——**这是待办项里最该先做的**
（用户明确要求过「必须无损往返写回」）。写回要做的是：
从 `CalibrationModel` + 值，重新生成 HEX / S19 / DCM，
并保证 parse→write→parse 结果一致。

### 3.4 授权体系（已定型，勿改结构）

Ed25519 签名**文本许可码**（≈300 字符，微信用一段消息收发，不是文件）。
同事报机器码 → 作者用 `tools/license-issuer/issuer.py` 签发 → 粘贴激活。
五道校验：验签 / 机器指纹 / 有效期 / 防时钟回拨 / 运行时长预算。
作者私钥离线保管、**不入 git、不打包**；软件内只硬编码公钥。
许可码前缀 `WF1` 是与项目名无关的线路常量，**改了要同步 issuer 与 codec**。

---

## 4. 纪律红线（违反任意一条都会腐蚀项目）

1. **依赖方向**由 `import-linter` + CI 守，不靠自觉。
   `apps → services → platform → core ← plugins`，只能向下。
2. **`core/` 内禁止出现具体格式名**（a2l / slx / hex / uds），出现即抽象泄漏。
3. **契约先行**，schema 变更算 breaking change（`contracts/` 慎改）。
4. **插件之间禁止互相 import**，需共享则上提 `core/` 或 `contracts/`。
   例：`ChangeSet` 就是因此上提的。
5. **UI 只消费 API 不做业务**，每面板独立 bundle + 独立状态切片。
6. **前端只通过 `127.0.0.1` 的 HTTP/WS 与内嵌 Python 服务通信**，
   不走 Tauri IPC 传业务数据——这样桌面单机等价于「本地服务+浏览器」，
   未来升级团队版零成本。

---

## 5. 下一步任务（可直接开工，三选一）

### 选项 A：CDM 写回 —— ✅ 已完成（P4，先做 HEX 一条路）

用户提过的硬要求是「能对 hex/dcm 直接操作修改后保存」，此前只实现了 parse。
已完成：
- 值 → 字节（`apply_values`，与 `bind_image` **严格反向**）、校验和重算
  （`recompute_checksums` 不再空转，算不了就抛错）、`Writer` 扩展点、
  `writer-hex` 插件、`ads writeback`
- 验收：一字不改时输出与原文件**字节级相同**；改一个量只有对应字节变

尚未做：
- DCM 解析与写回（**需要真实 `.dcm` 样本**，Vector 专有格式细节不公开，不要凭猜测写）

已补齐：S19 写回（checksum 是**一补数**，不是 HEX 的二补数，见坑 #13；
S0 头文本与终止记录类型不在镜像模型里，靠 `ctx["source_bytes"]` 从原文件还原）

### 选项 B：真实总线网关（CAN / DoIP）

P3 把刷写逻辑做完了，但**没有真实总线**。工作内容：
- 新增 `gateway-can` / `gateway-doip` 插件实现已有的 `Gateway` 协议
  （接口只传 `bytes`，不需要改动编排层与领域模型）
- 真机上联调需要台架与设备，无法在开发环境验证
- 需要真实 `.dcm` 或 ECU 的 Bootloader 手册才能写出对应配方

### 选项 C：P4 MATLAB 桥 + Tauri 桌面壳 + FastAPI

- MATLAB 集成用**文件契约**松耦合（`matlab -batch` 传参、写结果文件），不用 Engine API
- 桌面壳只走 `127.0.0.1` 的 HTTP/WS，不走 Tauri IPC 传业务数据
- 可以复用同一套许可码（`features` 加一项即可，公钥共用）

---

## 6. 血泪坑清单（按主题分组，逐条都实际踩过）

### 二进制格式布局（偏移差一位就整片错，且不报错）

12. **Intel HEX 数据切片从 `raw[4]` 开始**（`LL|AAAA|TT|DATA|CC`）。
    写成 `raw[5]` 会把 checksum 当数据、地址全错位，而且不报错。
13. **S-Record 的 checksum 是一补数（和 ≡ 0xFF）**，Intel HEX 是**二补数（和 ≡ 0x00）**。
    S2 只有 24 位地址，`0x8000_1234` 必须用 S3。
17. **MDF4 块布局**（以 asammdf 常量表为准）：通用头 24B（id/reserved/len/links_nr）；
    IDBLOCK 64B（id_file 8B `"MDF "`+填充、id_ver 4B @8、**HD 固定在偏移 64**）；
    HD 104B（first_dg @24、abs_time **int64 纳秒 @72**）；DG 64B（record_id_len @56）；
    CG 104B（record_id @72、cycles_nr @80、flags @88、samples_byte_nr @96）；
    CN 158B（channel_type @88、data_type @90、bit_offset @91、byte_offset @92、bit_count @96）；
    DT 数据从 +24 开始。**MDF4 永远小端。**
18. **MDF4 记录布局**：sorted = `[samples][invalidation]`（步长 = samples+inval）；
    unsorted = `[record_id][samples][invalidation]`（步长 += record_id_len）。
    **`cn_byte_offset` 相对样本区、不含 record_id**。数据类型 0/1/2/3 = byte/uint/int/real LE；
    位宽只支持 8/16/32/64。
19. **BLF 文件头是 144 字节**（结构化字段只占前 72，其余是保留填充）——
    只读 72 会把填充当对象区，解析出 0 帧且不报错。对象头 32B（V1），
    LOG_CONTAINER 内嵌未压缩对象流。SYSTEMTIME 元组顺序
    `(年,月,星期ISO,日,时,分,秒,毫秒)`——**星期和日期写反不会报错，只会静默错 7 天**。
20. **解析器读块链时 0 是合法地址**（IDBLOCK 就在 0）——空链接判断只能在
    `while addr:` 循环处做，**不能在 read helper 里 `addr <= 0` 一刀切**。

### 静默错误类（共同点：不报错，只是安静地错）

14. **`AddressSpace.read()` 对空洞填 0xFF**——读之前必须先 `covers()`，
    否则 Flash 擦除态会被当成真实标定值。**这是最危险的一类静默错误。**
21. **`bytes.ljust()` 不截断**（`b"长字符串".ljust(8)` 返回原长）——
    生成定长块字段要显式切片或拼接，否则写越界覆盖下一个块头。
16. **manifest 的 `handles` 只能写 `ArtifactKind` 的值**（mf4/blf/asc…），
    写文件后缀（mdf/dat）会整表校验失败 → 插件「不可用」，
    **且错误信息只在 registry 里，界面上看不出来**。
15. **manifest.toml 注释用 `#`，不是 Python 三引号**（写错 → 注册表静默判插件坏）。
    插件 id 与目录名是两回事（清单挂了会用目录名兜底，表现是「插件莫名不可用」）。

> **24 号坑（元坑）**：地址 0 合法、`ljust` 不截断、`handles` 写 kind 不写后缀
> 这三个坑有一个共同点——**不报错，只是安静地错**。
> 所以测试必须断言「**值对**」，而不是「没异常」。

### 比对与数值口径

8. **数值比对必须以物理值为准，不是原始值。**
    转换方法改过会让原始值整片变化而物理值未动，`_diff_values` 曾把这种情形
    误报成「数值改动」。原始值只作记录。
    整数类型从物理值反算原始值**必须量化**（`round`），否则精度有损。
4. **属性比对要报告 `unit`**，别并进 compu——否则「哪些量单位变了」就查不到了。
3. **`calibration.by_prefix` 只做段边界匹配 + 末段部分前缀**——
    A2L 符号大量扁平命名，别指望全段严格相等。
10. **绑定覆盖率要把「末段回退匹配」算进命中**——它只是路径写法不同，值是真拿到了。
9. **CDFX 两个坑**：实例是**树形**的（`ASAM.C.MAP` = 三层嵌套 `SW-INSTANCE`，
    路径必须沿祖先链拼）；取值时**不能跨进嵌套实例**（否则容器会多出子实例的值）。

### 工程纪律与测试

1. **`zlib.decompress(data, n)` 的 n 是 wbits(8–15)，不是长度上限**——
    传 `256*1024` 会抛 `zlib.error`。用 `decompressobj` + 检查 `unconsumed_tail`
    （防压缩炸弹被悄悄截断）。
2. **签发端 `issue(now=...)` 与校验端 `verify(now=...)` 必须都能注入时钟**，
    否则测试不可复现。且**时钟容差类用例的许可码必须早于 `NOW` 签发**
    （否则先撞 NotYetValid）。
5. **台账时间戳存到秒**；改字段必须先做表头迁移
    （`csv.DictWriter` 按 fieldnames 位置落值，错一列整行错位）。
6. **插件间共享类型必须上提 core**（如 `ChangeSet`），不靠自律。
7. **跑 `lint-imports` 必须带 PYTHONPATH=src**（见 §1.2 的 PowerShell 跑法）。
11. **Windows 下目录被占用时不能改名/删除**（WorkBuddy 的目录监听句柄）。
    绕法：新建目标目录 → 搬内容进去 → 旧目录就能删掉了。
22. **抽稀必须用 min/max 频带，不能等距抽样**——2ms 尖峰落在抽样点之间就永久消失，
    而工程上正是这些尖峰决定要不要停车检查。
23. **事件流（ASC/BLF）的「趋势」是报文速率**（每时间桶帧数）——
    没有 DBC 解不出信号值，别假装能解。

---

## 7. git / GitHub

- 仓库：`https://github.com/TopFrankOK/automotive-dev-support-tools`（**public**，MIT）
- 分支 `main`，tag：`p0` / `p1` / `p2` / `p3` / `p4`（每阶段一个，用户要求）
- 历史：`f758fa4`(P0 骨架+授权) → `dc39da5`(CDFX 值集) → `b7c037b`(P1 CDM 端到端)
  → `23e2975`(P2 测量数据分析) → `550e8ba`(P3 软件刷写) → P4 CDM 写回（HEX 一条路）
- **⚠️ 该项目有并发会话在活动**，动手前先 `git log` 看一眼，避免冲突
- 提交身份（仅 `--local`）：`TopFrankOK <TopFrankOK@users.noreply.github.com>`
- **`.gitattributes` 已钉住换行策略，且 `*.hex` 刻意判为文本**——
  Intel HEX 是纯文本，标成 binary 会让「比较两个 HEX 的差异」这个功能失去 diff。
  **不要改这一行。**
- **`.gitignore` 排除**：`keys/`、`*.key`、`private_key.hex`、`*.lic`、`licenses.csv`、
  `.env`、`.AutomotiveDevSupport/`。私钥和签发台账**切勿提交**。
- GitHub 认证只能用 PAT（账号密码自 2021 年起已被移除）。
  本环境 git 全局代理 `127.0.0.1:10808` 不通，需临时用沙箱代理覆盖：
  `git -c http.proxy=$PROXY -c https.proxy=$PROXY push`（**不要改用户全局配置**）。

---

## 8. 与用户协作时的注意事项

- 用户是汽车嵌入式软件开发工程师，沟通用**简体中文**，技术术语可直接用英文。
- 用户重视「**不报错，只是安静地错**」这类问题——汇报发现时要主动区分
  「报错的问题」和「静默错误」，后者优先级更高。
- 用户要求**每阶段打 tag**。
- 遇到需要用户决策的分叉（先做哪个域、要不要改架构），**先问再做**，
  不要擅自开工——这个项目的架构决策都是反复推敲出来的，推翻成本很高。

---

## 附：一句话状态

> P0 骨架+授权、P1 标定 CDM 端到端、P2 测量数据分析、P3 软件刷写、P4 CDM 写回（HEX）已完成（12 插件可用）；
> 待办：**DCM 写回**（需真实样本）、**真实总线网关**、P5 MATLAB 桥 + Tauri 壳 + FastAPI。
