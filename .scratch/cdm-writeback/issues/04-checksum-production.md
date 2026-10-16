# 04 — 校验和：从空转变成真算

**依赖**：01
**标签**：`type:feature` `domain:memory` `risk:high`

## 问题（这是本阶段最危险的一条）

`AddressSpace.recompute_checksums()` 现在是**空转**：

1. `parser_hex` / `parser_s19` 从不填充 `checksums`，所以根本没有规则可算；
2. `memory/model.py:285` 对「校验和存放地址不在镜像内」直接 `pass` 吞掉；
3. `_compute_checksum` 遇到未知算法 `return 0`。

三处叠加的结果：改完值不更新校验和、刷进 ECU 被拒 ——
**而整个过程不报任何错**。这正是 ARCHITECTURE 里点名的风险，目前没有任何机制拦得住。

## 做

1. `ChecksumSpec` 支持从外部显式构造并挂到 `AddressSpace.checksums`（一等公民）。
2. `recompute_checksums()`：
   - 区域不在镜像内 → **报错**（不再 `pass`），说清是哪条规则、哪个区间
   - `store_address` 为 None → 报错（无处可写等于没算）
   - `algorithm_ref` 非空（厂商私有算法）→ **报错**，不许瞎算一个错的填进去
   - 内置算法：sum8 / sum16 / sum32 / xor8 / crc32，按 `data_width`、`init_value`、`xor_out` 计算
3. 返回 `{规则名: 算出的值}`，并把值写进 `store_address`（按 `data_width` 与镜像字节序）。

## 不做

- 不实现厂商私有算法
- 不从 A2L 猜校验和位置（没有可靠依据；显式给出才可信）

## 验收

- sum32 区域改值后，存放地址处的字节随之改变
- 区域不在镜像内 → 报错，且**不静默跳过**
- `store_address` 越界 → 报错
- `algorithm_ref` 非空 → 报错
- 空 `checksums` 列表 → 返回空字典，不报错（没规则就不算，这是正常的）
- crc32 与已知值对得上（用例里给固定输入与期望值）
