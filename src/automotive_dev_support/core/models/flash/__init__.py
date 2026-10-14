"""刷写流程领域 —— 软件刷写（待实现）。

第三期建设。同样先固定设计约束。

## 已确定的设计约束

1. **刷写流程必须可配置，不能硬编码。**
   不同 ECU、不同 Bootloader 的时序都不一样。做成「刷写配方」
   （`plugins/flash-recipe-<ecu>/`），用状态机描述。
   适配一个新 ECU = 加一个配方文件，而不是改主程序。

2. **必须能断点续刷。** 刷到 80% 断了，重跑一遍会反复擦写 Flash，
   既慢又有风险。刷写前记录已完成段，重连后接着刷。
   这就是 `JobSpec.resumable` 存在的原因。

3. **审计日志要长期留存。**
   「谁在什么时间给哪台 ECU 刷了哪个版本的什么文件」——
   这在体系审核时一定会被问到，不能只写进临时日志。

4. **独立进程。** 刷写跑在硬件网关进程里，UI 与后端进程崩溃
   都不能影响正在进行的刷写；反过来总线驱动挂了也不能拖垮界面。
   进度通过 `EventBus` 的 `flash.progress` 推送。

5. **数据源复用 `models.memory.AddressSpace`。**
   刷写本质上就是「把地址空间镜像按段写进 ECU」——
   和 CDM 用的是同一个模型，不需要重复造。

## 计划中的实体

    FlashTarget      目标：ECU 标识、诊断 ID、当前版本、内存布局
    FlashStepKind    步骤类型：进编程会话、安全访问、擦除、传输、校验、复位
    FlashStep        一步：类型、参数、超时、重试策略、是否可取消
    FlashRecipe      配方：有序步骤集合 + 前置条件
    FlashProgress    进度：当前步、已完成段、已传输字节
    FlashRecord      审计记录：时间、操作者、目标、文件哈希、结果

`plugins/gateway-can` / `gateway-doip` 实现 `Gateway` 协议，
`plugins/flash-recipe-*` 提供配方，二者组合成一次刷写作业。
"""

from __future__ import annotations

__all__: list[str] = []
