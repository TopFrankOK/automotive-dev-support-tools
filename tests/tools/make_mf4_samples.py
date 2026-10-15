"""生成 MDF4 测试样例（tests/data/sample_v1.mf4）。

二进制格式的样例必须可复现 —— 手改十六进制编辑器改不出第二个，
所以生成器随仓库提交。改动块布局时先改这里，再跑测试。

样例内容（两个数据组，覆盖 sorted 与 unsorted 两条路径）：

    DG0 sorted     CG0：time(u64 ns) + EngineSpeed(f64) + CoolantTemp(i16)
                   100 条记录，10 ms 间隔
    DG1 unsorted   record_id_len=1；CG1(rid=1) 与 CG2(rid=2) 各 4 通道
                   （time/u32 id/u8 dlc/8B data），记录 1,2,1,2… 交错

用法：
    python tests/tools/make_mf4_samples.py
"""

from __future__ import annotations

import math
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

COMMON = struct.Struct("<4sIQQ")  # id, reserved, block_len, links_nr
Q = struct.Struct("<Q")
H = struct.Struct("<H")
I = struct.Struct("<I")
B = struct.Struct("<B")


class Builder:
    """顺序分配块地址，最后统一回填链接。"""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.patches: list[tuple[int, str, int]] = []  # (offset, kind, value)

    def alloc(self, size: int) -> int:
        addr = len(self.buf)
        self.buf.extend(b"\x00" * size)
        return addr

    def put(self, addr: int, data: bytes) -> None:
        self.buf[addr : addr + len(data)] = data

    def link(self, addr: int, offset: int, target: int) -> None:
        """在 addr+offset 处留一个 8 字节链接槽，稍后回填 target。"""
        self.patches.append((addr + offset, "Q", target))

    def tx(self, text: str) -> int:
        """##TX 文本块。"""
        raw = text.encode("utf-8")
        addr = self.alloc(24 + len(raw))
        self.put(addr, COMMON.pack(b"##TX", 0, 24 + len(raw), 0) + raw)
        return addr

    def seal(self) -> bytes:
        for offset, kind, value in self.patches:
            if kind == "Q":
                self.buf[offset : offset + 8] = Q.pack(value)
            else:
                raise AssertError(kind)
        return bytes(self.buf)


class AssertError(Exception):
    pass


def cn_block(
    b: Builder,
    *,
    next_ch: int,
    name_addr: int,
    unit_addr: int,
    channel_type: int,
    data_type: int,
    bit_count: int,
    byte_offset: int,
) -> int:
    addr = b.alloc(158)
    b.put(
        addr,
        COMMON.pack(b"##CN", 0, 158, 8)
        + Q.pack(next_ch)      # next
        + Q.pack(0)            # component
        + Q.pack(name_addr)    # name
        + Q.pack(0)            # source
        + Q.pack(0)            # conversion
        + Q.pack(0)            # data_block（VLSD 用）
        + Q.pack(unit_addr)    # unit
        + Q.pack(0)            # comment
        + bytes([channel_type, 0, data_type, 0])  # type/sync/data_type/bit_offset
        + I.pack(byte_offset)  # byte_offset
        + I.pack(bit_count)    # bit_count
        + I.pack(0)            # flags
        + I.pack(0)            # pos_invalidation_bit
        + bytes([0, 0])        # precision, reserved
        + b"\x00" * 48,        # 6 个 double 限值
    )
    return addr


def build(
    *,
    speed_offset: float = 0.0,
    spike_at: int | None = None,
    spike_delta: float = 0.0,
    n_sorted: int = 100,
) -> bytes:
    """构造一个 MDF4 文件字节串。

    speed_offset   EngineSpeed 整体偏移（模拟两次测量的差异）
    spike_at       在哪个样本上加一个尖峰（验证 min/max 抽稀不丢尖峰）
    """
    b = Builder()

    # -- IDBLOCK -------------------------------------------------------------
    # 布局：id_file(8, "MDF "+填充) / id_ver(4, "4.10") / id_prog(8) / 其余保留
    idb = b.alloc(64)
    id_block = (
        b"MDF " + b"\x00" * 4        # id_file：8 字节
        + b"4.10"                     # id_ver：4 字节
        + b"make_mf4" + b"\x00" * 4   # id_prog：8 字节
        + b"\x00" * 40                # 保留到 64
    )
    assert len(id_block) == 64, len(id_block)
    b.put(idb, id_block)

    # -- HD ------------------------------------------------------------------
    hd = b.alloc(104)
    b.put(hd, COMMON.pack(b"##HD", 0, 104, 6) + b"\x00" * (6 * 8))
    start_ns = int(
        datetime(2026, 10, 15, 4, 13, 41, 123456, tzinfo=timezone.utc).timestamp() * 1e9
    )
    b.put(
        hd + 72,
        Q.pack(start_ns) + H.pack(0) + H.pack(0) + bytes([0, 0, 0, 0]) + Q.pack(0) + Q.pack(0),
    )

    # ======================================================================
    # DG0：sorted 测量组
    # ======================================================================
    dg0 = b.alloc(64)
    cg0 = b.alloc(104)

    tx_time = b.tx("time")
    tx_speed = b.tx("EngineSpeed")
    tx_temp = b.tx("CoolantTemp")
    tx_rpm = b.tx("rpm")
    tx_degc = b.tx("degC")

    cn_time = cn_block(
        b, next_ch=0, name_addr=tx_time, unit_addr=tx_time,
        channel_type=2, data_type=1, bit_count=64, byte_offset=0,
    )
    cn_speed = cn_block(
        b, next_ch=0, name_addr=tx_speed, unit_addr=tx_rpm,
        channel_type=0, data_type=3, bit_count=64, byte_offset=8,
    )
    cn_temp = cn_block(
        b, next_ch=0, name_addr=tx_temp, unit_addr=tx_degc,
        channel_type=0, data_type=2, bit_count=16, byte_offset=16,
    )

    records = bytearray()
    for i in range(n_sorted):
        speed = 800.0 + 100.0 * math.sin(i / 10.0) + speed_offset
        if spike_at is not None and i == spike_at:
            speed += spike_delta
        records += Q.pack(start_ns + i * 10_000_000)
        records += struct.pack("<d", speed)
        records += struct.pack("<h", 900 + i)
    dt0 = b.alloc(24 + len(records))
    b.put(dt0, COMMON.pack(b"##DT", 0, 24 + len(records), 0) + bytes(records))

    b.put(
        dg0,
        COMMON.pack(b"##DG", 0, 64, 4) + b"\x00" * (4 * 8) + bytes([0]) + b"\x00" * 7,
    )
    b.link(dg0, 32, cg0)      # first_cg
    b.link(dg0, 40, dt0)      # data_block
    b.link(dg0, 48, 0)        # comment

    b.put(
        cg0,
        COMMON.pack(b"##CG", 0, 104, 6)
        + b"\x00" * (6 * 8)
        + Q.pack(0)            # record_id（sorted 不用）
        + Q.pack(n_sorted)     # cycles_nr
        + H.pack(0)            # flags
        + H.pack(0)            # path_separator
        + I.pack(0)            # reserved
        + I.pack(18)           # samples_byte_nr = 8 + 8 + 2
        + I.pack(0),           # invalidation_bytes_nr
    )
    b.link(cg0, 24, 0)        # next_cg
    b.link(cg0, 32, cn_time)  # first_ch
    b.link(cg0, 40, 0)        # acq_name
    b.link(cg0, 48, 0)        # acq_source
    b.link(cg0, 56, 0)        # first_sample_reduction
    b.link(cg0, 64, 0)        # comment
    b.put(cn_time + 24, Q.pack(cn_speed))
    b.put(cn_speed + 24, Q.pack(cn_temp))

    # ======================================================================
    # DG1：unsorted 总线组（record_id_len=1，两个 CG 交错）
    # ======================================================================
    dg1 = b.alloc(64)
    cg1 = b.alloc(104)
    cg2 = b.alloc(104)

    tx_id = b.tx("CAN_ID")
    tx_dlc = b.tx("DLC")
    tx_data = b.tx("DataBytes")

    def bus_channels(first_addr_holder: list[int]) -> list[int]:
        addrs = []
        specs = [
            ("time", 2, 1, 64, 0),
            ("CAN_ID", 0, 1, 32, 8),
            ("DLC", 0, 0, 8, 12),
            ("DataBytes", 0, 0, 64, 13),
        ]
        for name, ctype, dtype, bits, off in specs:
            addr = cn_block(
                b, next_ch=0,
                name_addr=b.tx(name), unit_addr=tx_id if name == "CAN_ID" else b.tx(""),
                channel_type=ctype, data_type=dtype, bit_count=bits, byte_offset=off,
            )
            addrs.append(addr)
        for a, nxt in zip(addrs, addrs[1:]):
            b.put(a + 24, Q.pack(nxt))
        return addrs

    ch1 = bus_channels([])
    ch2 = bus_channels([])

    frames = bytearray()
    n_per_cg = 10
    for i in range(n_per_cg):
        # CG1（rid=1）的记录：[record_id][time][id][dlc][data]
        frames += bytes([1])
        frames += Q.pack(start_ns + 1_000_000_000 + i * 20_000_000)
        frames += I.pack(0x100 + i)
        frames += bytes([8])
        frames += bytes(range(1, 9))
        # CG2（rid=2）的记录
        frames += bytes([2])
        frames += Q.pack(start_ns + 1_000_000_000 + i * 20_000_000 + 5_000_000)
        frames += I.pack(0x200 + i)
        frames += bytes([4])
        frames += bytes([0xAA, 0xBB, 0xCC, 0xDD, 0, 0, 0, 0])
    dt1 = b.alloc(24 + len(frames))
    b.put(dt1, COMMON.pack(b"##DT", 0, 24 + len(frames), 0) + bytes(frames))

    b.put(
        dg1,
        COMMON.pack(b"##DG", 0, 64, 4) + b"\x00" * (4 * 8) + bytes([1]) + b"\x00" * 7,
    )
    b.link(dg1, 24, 0)        # next_dg
    b.link(dg1, 32, cg1)      # first_cg
    b.link(dg1, 40, dt1)      # data_block
    b.link(dg1, 48, 0)        # comment

    for cg, rid, first_ch, n in ((cg1, 1, ch1[0], n_per_cg), (cg2, 2, ch2[0], n_per_cg)):
        b.put(
            cg,
            COMMON.pack(b"##CG", 0, 104, 6)
            + b"\x00" * (6 * 8)
            + Q.pack(rid)       # record_id
            + Q.pack(n)         # cycles_nr
            + H.pack(1 << 1)    # flags：bus event（未排序）
            + H.pack(0)         # path_separator
            + I.pack(0)         # reserved
            + I.pack(21)        # samples_byte_nr = 8 + 4 + 1 + 8
            + I.pack(0),        # invalidation_bytes_nr
        )
        b.link(cg, 32, first_ch)
        b.link(cg, 40, 0)
        b.link(cg, 48, 0)
        b.link(cg, 56, 0)
        b.link(cg, 64, 0)
    b.put(cg1 + 24, Q.pack(cg2))  # CG 链：cg1 → cg2

    # HD 指向第一个 DG；DG0 指向 DG1
    b.link(hd, 24, dg0)
    b.put(dg0 + 24, Q.pack(dg1))

    return b.seal()


def main() -> None:
    out_dir = Path("tests/data")
    out_dir.mkdir(parents=True, exist_ok=True)

    # v1：基线。v2：转速整体 +50，第 50 样本再加 200 尖峰
    # （验证 min/max 抽稀留得住尖峰、align 差异统计抓得到它）。
    variants = {
        "sample_v1.mf4": {},
        "sample_v2.mf4": {"speed_offset": 50.0, "spike_at": 50, "spike_delta": 200.0},
    }
    for name, kwargs in variants.items():
        blob = build(**kwargs)
        (out_dir / name).write_bytes(blob)
        print(f"written {out_dir / name} ({len(blob)} bytes)")


if __name__ == "__main__":
    main()
