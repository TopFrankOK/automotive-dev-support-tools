# 03 — 值 → 字节（读的逆函数）

**依赖**：无（可与 01/02 并行）
**标签**：`type:feature` `domain:calibration` `risk:high`

## 问题

`values.py:368` 的 `_read_characteristic` 没有逆函数。
没有它，写回就只能手工拼字节 —— 字节序、数据类型、ASCII、定/变长全部要重来一遍，
而且必须和读的时候**完全一致**，否则往返就不是无损的。

## 做

1. `_write_characteristic(char, image, prefix) -> bool` —— 与读严格对称：
   - ASCII 量按字节写入
   - 其余按 `_STRUCT_FMT` + `prefix`（字节序）打包
   - 写之前必须 `image.covers()` 检查；不覆盖返回 False，**不猜**
2. `apply_values(model, image, byte_order="big") -> WritebackResult`：
   把模型上挂着的 `raw_values` 逐个写进镜像，返回成功/跳过的清单与原因。
3. 跳过的量必须**逐个报出原因**（无地址 / 元素个数未知 / 地址不在镜像内 / 类型不支持），
   不许只报一个总数。

## 不做

- 不做「全量重写」（会把未改动的行也重排，违反 §6.3）
- 不改动 `bind_image()` 的行为

## 验收

- bind → 改一个量的 raw_values → apply → 读回，值一致
- 未涉及的其他量字节完全不变
- 地址不在镜像内的量被报出，且镜像未被改动
- ASCII 量往返正确
- 小端（byte_order="little"）往返正确
- 跳过原因里能读到具体符号路径
