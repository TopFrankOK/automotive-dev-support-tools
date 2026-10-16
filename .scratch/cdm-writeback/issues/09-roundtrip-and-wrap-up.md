# 09 — 往返测试与收尾

**依赖**：06, 07, 08
**标签**：`type:test` `type:docs` `risk:low`

## 做

1. **往返测试**（本阶段的核心资产，也是目前唯一完全缺失的一类测试）：
   `parse → 写回 → parse`，断言：
   - 一字不改时，输出与输入**字节级相同**
   - 改一个量后，只有对应字节变，其余逐字节相同
   - 未改动的行**逐字符**相同（行序、行尾、大小写）
   - HEX 与 S19 各跑一遍
2. `.importlinter` 登记新插件；确认 4 kept / 0 broken
3. 文档：README 加写回命令；ARCHITECTURE 补 Writer 扩展点；HANDOFF 更新阶段状态；
   GLOSSARY 补「无损往返 / 记录边界 / 一补数」等词
4. 复核 `recompute_checksums()` 不再有静默吞分支

## 验收

- 往返测试对 `tests/data/` 下全部样例成立（hex、s19 × v1/v2）
- 契约 4 kept / 0 broken
- 全量测试全绿
- 文档里的命令实跑能通
