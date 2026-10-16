# 06 — writer-hex 插件

**依赖**：02, 05
**标签**：`type:plugin` `risk:medium`

## 做

`plugins/writer_hex/`：把 `AddressSpace` 序列化成 Intel HEX 文本。

- 每行 ≤ 0xFF 字节数据（规范上限），按 `iter_blocks()` 给的行边界切
- 数据记录 `00`；需要时插入扩展线性地址记录 `04`（地址 >0xFFFF）；结尾写 EOF `:00000001FF`
- 行 checksum：`(sum(所有字节) & 0xFF) == 0`（与 `parser_hex:67` 的校验完全一致）
- 大写十六进制（与主流工具一致）

## 验收

- **一字不改时，输出字节 == 输入字节**（§6.3，最要紧的一条）
- 改一个字节后，只有那一行变，其它行逐字符不变（§6.2）
- 地址 >0xFFFF 时产出 `04` 记录，且能再被 `parser_hex` 读回同样字节
- 每行数据 ≤ 0xFF
- 每行 checksum 合法（能被自己的解析器接受）
- 空镜像产出合法的空文件（只有 EOF 行）
- 插件被注册表发现，能力含 `writer`
