# 05 — Writer 扩展点

**依赖**：02
**标签**：`type:contract` `risk:medium`

## 问题

`core/ports` 有 Parser / Analyzer / Exporter / View / Gateway / Recipe，没有 Writer。
写回不能塞进 Exporter：**Exporter 产出的是给人看的报告，Writer 产出的是要刷进 ECU 的文件**。
两者的后果完全不同，混在一个扩展点里迟早出事。

## 做

在 `core/ports/plugin.py` 增加 `Writer` 协议（**只增不改**，已有协议一字不动）：

```python
@runtime_checkable
class Writer(Protocol):
    writer_id: str
    def serialize(self, image: AddressSpace, ctx: dict[str, Any]) -> bytes: ...
```

- `serialize` 产出**完整文件的字节**（含 EOF 记录），由调用方交给 `DataSource.write_all()` 原子落盘
- 块的切分由 core 的 `iter_blocks()` 完成，插件只管编码文本行 —— 分工与 P3 的字节缝一致
- 能力值：`Capability` 枚举加一项（注意：改枚举是**新增值**，不破坏已有插件）
- 与 `PluginRegistry` 的发现机制对齐（manifest `capabilities = ["writer"]`）

## 不做

- 不做 `.bin`（没有记录边界与地址元信息，做了就是猜）

## 验收

- `Writer` 可被 `runtime_checkable` 判定
- 只实现 `serialize` 的类算 Writer；少一个方法不算
- 已有 6 个协议与 9 个旧插件的行为完全不变（test_ports 全绿）
- 核心里查不到 hex / s19 等格式名
