# 07 — writer-s19 插件

**依赖**：02, 05
**标签**：`type:plugin` `risk:medium`

## 做

`plugins/writer_s19/`：把 `AddressSpace` 序列化成 Motorola S-Record 文本。

- 按地址宽度自动选 S1（16 位）/ S2（24 位）/ S3（32 位）；一段之内不混用
- 结尾 `S9` / `S8` / `S7`（与地址宽度对应）
- checksum 为**一补数**（`0xFF - (sum & 0xFF)`）—— 与 HEX 的二补数不同，
  这是 `HANDOFF` 里点名踩过的坑，别照抄 HEX 的算法
- 大写十六进制

## 验收

- 一字不改时输出字节 == 输入字节（§6.3）
- 改一个字节后只有那一行变（§6.2）
- S1/S2/S3 三种地址宽度各自能往返
- checksum 为一补数，能被 `parser_s19` 接受
- 段落地址宽度不一致时分别出不同记录类型，读回字节一致
- 插件被注册表发现，能力含 `writer`
