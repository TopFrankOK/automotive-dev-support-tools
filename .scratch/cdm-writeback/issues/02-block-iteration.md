# 02 — 按边界产出块

**依赖**：01
**标签**：`type:feature` `domain:memory` `risk:medium`

## 问题

writer 插件需要一份「按原文件行边界切好的 (地址, 字节) 块」。
这块逻辑是格式无关的，放插件里就会在两个插件里各写一遍（且不允许互相 import）。

## 做

`AddressSpace.iter_blocks(max_bytes: int | None = None) -> Iterator[tuple[int, bytes]]`

- 默认按 `Segment.boundaries` 切分（沿用原文件布局）
- 边界缺失时（段上没有 boundaries），按 `max_bytes` 切；`max_bytes` 为 None 时整段一块
- 保证**覆盖镜像全部字节且不重叠**：写回必须能重建完整镜像

## 验收

- 有边界的段按边界切
- 无边界的段按 max_bytes 切；最后一块可以短
- 所有块拼接起来 == 原段字节（逐字节）
- 块的地址单调递增
- 空洞不产生块（不发明不存在的字节）
