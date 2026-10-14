"""记录流 → 内存镜像的装配器。

**为什么它在 core 里而不在某个插件里：** Intel HEX、Motorola S19、
未来的 ELF section 表，本质都是「一串 (地址, 字节) 记录」。
装配逻辑（合并连续段、发现重叠、保留未识别字节）是格式无关的，
放进 core 才能被多个解析器共用而不违反「插件之间禁止互相 import」。

**重叠怎么办：** 同一地址被写两次，文件本身就是坏的。
这里按「后者覆盖前者」处理并记一条警告 —— 与硬件刷写的行为一致。
绝不静默：标定场景里「以为加载的是 A 其实加载的是 B」是数据事故。
"""

from __future__ import annotations

from automotive_dev_support.core.models.memory.model import AddressSpace, Segment

__all__ = ["ImageAssembler"]


class ImageAssembler:
    """把地址/字节块装配成 `AddressSpace`。"""

    def __init__(self) -> None:
        self._chunks: list[tuple[int, bytes, str]] = []
        self.warnings: list[str] = []

    def add(self, address: int, data: bytes, origin: str = "") -> None:
        """登记一块数据。`origin` 记录它来自哪里（如 "line 42"），写回时溯源用。"""
        if not data:
            return
        if address < 0:
            self.warnings.append(f"忽略一段负数地址的数据（{origin or '未知来源'}）")
            return
        self._chunks.append((address, bytes(data), origin))

    def build(self) -> AddressSpace:
        """产出镜像。重复调用安全（内部状态不被消费）。

        装配过程中的警告（重叠等）留在 `self.warnings` 上，
        由调用方决定是上报、忽略还是升级为错误。
        """
        ordered = sorted(self._chunks, key=lambda c: (c[0], c[2]))
        segments: list[Segment] = []
        start: int | None = None
        buf = bytearray()
        end = 0
        origin = ""

        for addr, data, chunk_origin in ordered:
            if start is None:
                start, buf, end, origin = addr, bytearray(data), addr + len(data), chunk_origin
                continue
            if addr > end:
                segments.append(Segment(start=start, data=bytes(buf), origin=origin))
                start, buf, end, origin = addr, bytearray(data), addr + len(data), chunk_origin
                continue
            if addr < end:
                self.warnings.append(
                    f"地址 0x{addr:X} 的数据与已加载内容重叠"
                    f"（后者覆盖前者，来源 {chunk_origin or '未知'}）"
                )
            overlap = end - addr
            buf[addr - start : end - start] = data[:overlap]
            tail = data[overlap:]
            if tail:
                buf.extend(tail)
                end = addr + len(data)

        if start is not None:
            segments.append(Segment(start=start, data=bytes(buf), origin=origin))

        return AddressSpace(segments=segments)
