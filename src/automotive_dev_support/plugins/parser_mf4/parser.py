"""MDF4（ASAM Measurement Data Format 4）解析器插件。

## 只走块链，不扫全文件

MF4 是块链结构：IDBLOCK(64B) → HD → DG → CG → CN，数据在 DT 块里。
本插件从 HD 的 `hd_dg_first` 出发，顺链读块，**只碰元数据块**，
GB 级文件也能毫秒级完成索引。通道数据不读 —— 只记录
「在哪个偏移、记录多长、怎么解」（`DataLocation`），
取值由用例层按需发起（`services.measurement.load_series`）。

## 记录布局（规范确认过的两个事实）

    sorted    [samples][invalidation]          步长 = samples + invalidation
    unsorted  [record_id][samples][invalidation] 步长 = record_id_len + samples + invalidation

`cn_byte_offset` 相对**样本区**起算（不含 record_id）——
MDF3 规范的「（总）字节偏移量应用于纯记录数据，比如没有记录ID」与
asammdf 的实现一致。sorted 组 record_id_len 为 0，两种情形公式统一。

## 能力边界（宁缺勿假）

- **sorted 组**：完整懒加载，支持跨多个 DT 块（`DataLocation.spans`）。
- **unsorted 组**（总线日志）：顺序扫描按 record_id 归属，
  **解析时直接抽取值**（记录不连续，无法懒加载），
  单数据组超过 `_MAX_UNSORTED_BYTES` 字节则整组只建索引不取值并告警。
- **不支持**：DZ 压缩块、LD 块、VLSD/MLSD 通道、虚拟/同步通道、
  位压缩通道（`bit_count % 8 != 0`）、非 8/16/32/64 位宽、
  字符串通道、数组/结构组合通道、通道转换（只取单位不换算）。
  这些情形一律**显式告警并跳过**，不静默产出错数。
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from automotive_dev_support.core.models.measurement import (
    ChannelGroup,
    DataLocation,
    DataTypeSpec,
    Measurement,
    SignalKind,
    SignalSeries,
    ValueDtype,
)
from automotive_dev_support.core.ports.plugin import ParseOutcome
from automotive_dev_support.core.skeleton import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    DataSource,
    JobContext,
)

__all__ = ["Mdf4Parser"]

# -- 块 ID --------------------------------------------------------------------
_IDBLOCK_SIZE = 64
_HD = b"##HD"
_DG = b"##DG"
_CG = b"##CG"
_CN = b"##CN"
_TX = b"##TX"
_DT = b"##DT"

# -- 固定块头偏移（MDF4 通用头：id(4) + reserved(4) + len(8) + links_nr(8)）--
_LEN_AT = 8
_LINKS_NR_AT = 16

# HD：links_nr 之后是 6 个链接 + abs_time
_HD_FIRST_DG_AT = 24
_HD_ABS_TIME_AT = 72
_HD_TZ_OFFSET_AT = 80

# DG：next / first_cg / data_block / comment / record_id_len(1B)
_DG_NEXT_AT = 24
_DG_FIRST_CG_AT = 32
_DG_DATA_AT = 40
_DG_RECORD_ID_LEN_AT = 56

# CG：next / first_ch / acq_name / acq_source / first_sr / comment /
#     record_id / cycles_nr / flags / samples_byte_nr / invalidation_bytes_nr
_CG_NEXT_AT = 24
_CG_FIRST_CH_AT = 32
_CG_ACQ_NAME_AT = 40
_CG_RECORD_ID_AT = 72
_CG_CYCLES_AT = 80
_CG_FLAGS_AT = 88
_CG_SAMPLES_BYTES_AT = 96
_CG_INVAL_BYTES_AT = 100
_CG_HEADER_SIZE = 104

# CN：next / component / name / source / conversion / data_block / unit / comment
#     channel_type(1B) / sync_type(1B) / data_type(1B) / bit_offset(1B)
#     byte_offset(4B) / bit_count(4B) / flags(4B) / pos_invalidation_bit(4B)
_CN_NEXT_AT = 24
_CN_NAME_AT = 40
_CN_UNIT_AT = 72
_CN_TYPE_AT = 88
_CN_DATA_TYPE_AT = 90
_CN_BIT_OFFSET_AT = 91
_CN_BYTE_OFFSET_AT = 92
_CN_BIT_COUNT_AT = 96
_CN_HEADER_SIZE = 158

# -- MDF4 枚举 ----------------------------------------------------------------
#: 数据类型：0=byte 1=uint LE 2=int LE 3=real LE（MDF4 数值型只有小端）
_DATA_TYPE_BYTE = 0
_DATA_TYPE_UINT = 1
_DATA_TYPE_INT = 2
_DATA_TYPE_REAL = 3
_DATA_TYPE_STRING_LATIN = 7
_DATA_TYPE_STRING_UTF8 = 8
_SUPPORTED_DATA_TYPES = {
    _DATA_TYPE_BYTE: ValueDtype.UINT,
    _DATA_TYPE_UINT: ValueDtype.UINT,
    _DATA_TYPE_INT: ValueDtype.INT,
    _DATA_TYPE_REAL: ValueDtype.FLOAT,
}

#: 通道类型
_CH_VALUE = 0
_CH_MASTER = 2

#: 通道组标志
_FLAG_CG_VLSD = 1
_FLAG_CG_BUS_EVENT = 1 << 1
_FLAG_CG_REMOTE_MASTER = 1 << 3

#: unsorted 组单数据组的取值上限（字节）。超过只建索引不取值。
_MAX_UNSORTED_BYTES = 256 << 20

_U16 = struct.Struct("<H").unpack_from
_U32 = struct.Struct("<I").unpack_from
_U64 = struct.Struct("<Q").unpack_from
_I16 = struct.Struct("<h").unpack_from
_I64 = struct.Struct("<q").unpack_from

_WIDTH_TO_BYTES = {8: 1, 16: 2, 32: 4, 64: 8}


def _stem(uri: str) -> str:
    tail = uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail


class _Chain:
    """按块链顺序读块的游标。所有读取走 `read_at`，绝不整体载入。"""

    def __init__(self, source: DataSource) -> None:
        self._source = source
        self.size = source.artifact_ref().size or 0

    def block(self, addr: int, need: int) -> bytes | None:
        """读块头及固定部分。地址为负或越界时返回 None。

        注意 0 是合法地址（IDBLOCK 就在文件头）—— 空链接的判断
        由调用方的 `while addr:` 循环负责，不在这里做。
        """
        if addr < 0 or addr + need > self.size:
            return None
        return self._source.read_at(addr, need)

    def text(self, addr: int) -> str:
        """读 ##TX 文本块。失败返回空串（名称缺失不致命）。"""
        if addr <= 0:
            return ""
        head = self.block(addr, 24)
        if head is None or head[:4] != _TX:
            return ""
        (block_len,) = _U64(head, _LEN_AT)
        body = self.block(addr + 24, max(0, block_len - 24))
        if body is None:
            return ""
        raw = body.split(b"\x00", 1)[0]
        for encoding in ("utf-8", "latin-1"):
            try:
                return raw.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return ""


class Mdf4Parser:
    """`core.ports.Parser` 的实现。产出事件/连续混合的 Measurement。"""

    kinds = frozenset({ArtifactKind.MDF4})

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        artifact = Artifact(
            kind=ArtifactKind.MDF4,
            role=ArtifactRole.MEASUREMENT,
            ref=source.artifact_ref(),
            producer="parser-mf4",
        )
        warnings: list[str] = []
        measurement = Measurement(label=_stem(source.uri))
        chain = _Chain(source)

        # -- IDBLOCK --------------------------------------------------------
        idb = chain.block(0, _IDBLOCK_SIZE)
        if idb is None or idb[:4] != b"MDF ":
            warnings.append("不是 MDF4 文件（IDBLOCK 签名不是 'MDF '）")
            artifact.meta = {"groups": 0, "signals": 0, "finalized": False}
            return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)
        version = idb[8:12].decode("ascii", errors="replace").strip()
        if not version.startswith("4"):
            warnings.append(
                f"文件声明 MDF 版本 {version!r}，本解析器只支持 4.x，"
                f"块结构不按 4.x 解释"
            )
            artifact.meta = {"version": version, "groups": 0, "signals": 0}
            return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)

        # -- HD -------------------------------------------------------------
        hd = chain.block(_IDBLOCK_SIZE, 104)
        if hd is None or hd[:4] != _HD:
            warnings.append("第 64 字节处不是 ##HD 块（文件可能损坏或不是 MDF4）")
            artifact.meta = {"version": version, "groups": 0, "signals": 0}
            return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)
        (first_dg,) = _U64(hd, _HD_FIRST_DG_AT)
        (abs_time_ns,) = _I64(hd, _HD_ABS_TIME_AT)
        (tz_offset_min,) = _I16(hd, _HD_TZ_OFFSET_AT)
        if abs_time_ns > 0:
            try:
                measurement.start_time = datetime.fromtimestamp(
                    abs_time_ns / 1e9, tz=timezone.utc
                )
            except (OverflowError, OSError, ValueError):
                warnings.append(f"hd_abs_time {abs_time_ns} 无法换算成日期，已忽略")

        # -- DG 链 ----------------------------------------------------------
        stats = {
            "data_groups": 0,
            "channel_groups": 0,
            "signals": 0,
            "skipped_groups": 0,
        }
        dg_addr = first_dg
        seen_dg: set[int] = set()
        while dg_addr:
            if dg_addr in seen_dg:
                warnings.append(f"数据组链在偏移 {dg_addr} 处成环，停止遍历")
                break
            seen_dg.add(dg_addr)
            dg = chain.block(dg_addr, 64)
            if dg is None or dg[:4] != _DG:
                warnings.append(f"偏移 {dg_addr} 处不是 ##DG 块，停止遍历数据组")
                break
            (next_dg,) = _U64(dg, _DG_NEXT_AT)
            (first_cg,) = _U64(dg, _DG_FIRST_CG_AT)
            (data_block,) = _U64(dg, _DG_DATA_AT)
            record_id_len = dg[_DG_RECORD_ID_LEN_AT]
            stats["data_groups"] += 1

            self._parse_data_group(
                chain=chain,
                source=source,
                dg_index=stats["data_groups"],
                first_cg=first_cg,
                data_block=data_block,
                record_id_len=record_id_len,
                measurement=measurement,
                warnings=warnings,
                stats=stats,
            )
            dg_addr = next_dg

        if stats["data_groups"] == 0:
            warnings.append("文件里没有数据组（##DG）—— 只有元数据，没有测量内容")

        artifact.label = _stem(source.uri)
        artifact.meta = {
            "version": version,
            "start_time_ns": abs_time_ns,
            "tz_offset_min": tz_offset_min,
            **stats,
            "signals_indexed": measurement.signal_count(),
            "signals_loaded": measurement.loaded_count(),
        }
        return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)

    # ------------------------------------------------------------------
    # 数据组
    # ------------------------------------------------------------------

    def _parse_data_group(
        self,
        *,
        chain: _Chain,
        source: DataSource,
        dg_index: int,
        first_cg: int,
        data_block: int,
        record_id_len: int,
        measurement: Measurement,
        warnings: list[str],
        stats: dict[str, int],
    ) -> None:
        # 先收集 CG 元数据（unsorted 需要全组信息才能顺序扫描）
        cgs: list[dict] = []
        cg_addr = first_cg
        seen: set[int] = set()
        while cg_addr:
            if cg_addr in seen:
                warnings.append(f"数据组 {dg_index} 的通道组链在 {cg_addr} 处成环，停止")
                break
            seen.add(cg_addr)
            raw = chain.block(cg_addr, _CG_HEADER_SIZE)
            if raw is None or raw[:4] != _CG:
                warnings.append(f"偏移 {cg_addr} 处不是 ##CG 块，停止遍历该数据组的通道组")
                break
            (next_cg,) = _U64(raw, _CG_NEXT_AT)
            (first_ch,) = _U64(raw, _CG_FIRST_CH_AT)
            (acq_name_addr,) = _U64(raw, _CG_ACQ_NAME_AT)
            (record_id,) = _U64(raw, _CG_RECORD_ID_AT)
            (cycles_nr,) = _U64(raw, _CG_CYCLES_AT)
            (cg_flags,) = _U16(raw, _CG_FLAGS_AT)
            (samples_bytes,) = _U32(raw, _CG_SAMPLES_BYTES_AT)
            (inval_bytes,) = _U32(raw, _CG_INVAL_BYTES_AT)
            cgs.append(
                {
                    "addr": cg_addr,
                    "name": chain.text(acq_name_addr),
                    "first_ch": first_ch,
                    "record_id": record_id,
                    "cycles_nr": cycles_nr,
                    "flags": cg_flags,
                    "samples_bytes": samples_bytes,
                    "inval_bytes": inval_bytes,
                    "channels": [],
                    "skipped": False,
                }
            )
            cg_addr = next_cg

        if not cgs:
            warnings.append(f"数据组 {dg_index} 里没有通道组（##CG）")
            return

        unsorted = record_id_len > 0
        # DT 链：数据可能跨多个块
        spans = self._dt_spans(chain, data_block, warnings)

        if unsorted:
            self._parse_unsorted(
                chain=chain,
                source=source,
                dg_index=dg_index,
                record_id_len=record_id_len,
                spans=spans,
                cgs=cgs,
                measurement=measurement,
                warnings=warnings,
                stats=stats,
            )
            return

        # -- sorted：懒加载索引 --------------------------------------------
        total_bytes = sum(length for _off, length in spans)
        for cg in cgs:
            self._index_cg(
                chain=chain,
                dg_index=dg_index,
                cg=cg,
                spans=spans,
                total_bytes=total_bytes,
                measurement=measurement,
                warnings=warnings,
                stats=stats,
            )

    def _index_cg(
        self,
        *,
        chain: _Chain,
        dg_index: int,
        cg: dict,
        spans: list[tuple[int, int]],
        total_bytes: int,
        measurement: Measurement,
        warnings: list[str],
        stats: dict[str, int],
    ) -> None:
        if cg["flags"] & _FLAG_CG_VLSD:
            warnings.append(
                f"数据组 {dg_index} 的通道组（偏移 {cg['addr']}）含 VLSD 变长通道，"
                f"暂不支持，已跳过"
            )
            stats["skipped_groups"] += 1
            return
        if cg["flags"] & _FLAG_CG_REMOTE_MASTER:
            warnings.append(
                f"数据组 {dg_index} 的通道组（偏移 {cg['addr']}）是远程主通道组"
                f"（数据在别处），暂不支持，已跳过"
            )
            stats["skipped_groups"] += 1
            return

        channels = self._read_channels(chain, cg, warnings, dg_index)
        if not channels:
            # 一个可用通道都没有的组不值得建档（告警已在 _read_channels 里出过）
            stats["skipped_groups"] += 1
            return
        samples_bytes = cg["samples_bytes"]
        inval_bytes = cg["inval_bytes"]
        stride = samples_bytes + inval_bytes
        if stride <= 0:
            warnings.append(
                f"数据组 {dg_index} 通道组（偏移 {cg['addr']}）记录长度为 0，已跳过"
            )
            stats["skipped_groups"] += 1
            return

        # 记录条数：优先 cycles_nr；与数据区容量不符时以容量为准并告警
        capacity = total_bytes // stride
        count = cg["cycles_nr"]
        if count > capacity:
            warnings.append(
                f"通道组（偏移 {cg['addr']}）声明 {count} 条记录，"
                f"但数据区只有 {capacity} 条（文件可能被截断），按 {capacity} 条建索引"
            )
            count = capacity
        elif count == 0 and capacity > 0:
            count = capacity

        master: SignalSeries | None = None
        signals: list[SignalSeries] = []
        uri = chain._source.uri
        for ch in channels:
            series = SignalSeries(
                path=ch["name"] or f"ch{ch['addr']}",
                name=ch["name"],
                unit=ch["unit"],
                kind=SignalKind.CONTINUOUS,
                group=len(measurement.groups),
                spec=ch["spec"],
                location=DataLocation(
                    uri=uri,
                    start=spans[0][0] if spans else 0,
                    stride=stride,
                    offset=ch["byte_offset"],
                    count=count,
                    spec=ch["spec"],
                    spans=list(spans),
                ),
            )
            if ch["is_master"]:
                if master is None:
                    master = series
                else:
                    warnings.append(
                        f"通道组（偏移 {cg['addr']}）有多个时间主通道，"
                        f"只把 {ch['name']!r} 当主通道"
                    )
                    signals.append(series)
            else:
                signals.append(series)

        if master is None:
            warnings.append(
                f"数据组 {dg_index} 的通道组（偏移 {cg['addr']}）没有时间主通道，"
                f"秒时间轴不可用（原始索引仍在）"
            )

        measurement.groups.append(
            ChannelGroup(
                index=len(measurement.groups),
                name=cg["name"] or f"group {len(measurement.groups)}",
                master=master,
                signals=signals,
            )
        )
        stats["channel_groups"] += 1
        stats["signals"] += len(signals) + (1 if master else 0)

    def _read_channels(
        self, chain: _Chain, cg: dict, warnings: list[str], dg_index: int
    ) -> list[dict]:
        """读 CN 链。返回 [{name, unit, spec, byte_offset, is_master, addr}]。"""
        out: list[dict] = []
        ch_addr = cg["first_ch"]
        seen: set[int] = set()
        samples_bytes = cg["samples_bytes"]
        while ch_addr:
            if ch_addr in seen:
                warnings.append(f"通道链在 {ch_addr} 处成环，停止")
                break
            seen.add(ch_addr)
            raw = chain.block(ch_addr, _CN_HEADER_SIZE)
            if raw is None or raw[:4] != _CN:
                warnings.append(f"偏移 {ch_addr} 处不是 ##CN 块，停止遍历该组通道")
                break
            (next_ch,) = _U64(raw, _CN_NEXT_AT)
            (name_addr,) = _U64(raw, _CN_NAME_AT)
            (unit_addr,) = _U64(raw, _CN_UNIT_AT)
            ch_type = raw[_CN_TYPE_AT]
            data_type = raw[_CN_DATA_TYPE_AT]
            bit_offset = raw[_CN_BIT_OFFSET_AT]
            (byte_offset,) = _U32(raw, _CN_BYTE_OFFSET_AT)
            (bit_count,) = _U32(raw, _CN_BIT_COUNT_AT)
            name = chain.text(name_addr)

            if ch_type not in (_CH_VALUE, _CH_MASTER):
                warnings.append(
                    f"通道 {name or ch_addr} 类型是 {ch_type}"
                    f"（非数值/主通道，可能是 VLSD/虚拟/同步通道），已跳过"
                )
                ch_addr = next_ch
                continue
            if data_type not in _SUPPORTED_DATA_TYPES:
                kind_name = (
                    "字符串"
                    if data_type in (_DATA_TYPE_STRING_LATIN, _DATA_TYPE_STRING_UTF8)
                    else f"类型码 {data_type}"
                )
                warnings.append(
                    f"通道 {name or ch_addr} 是{kind_name}，暂不支持解码，已跳过"
                )
                ch_addr = next_ch
                continue
            if bit_offset != 0:
                warnings.append(
                    f"通道 {name or ch_addr} 位偏移 {bit_offset} 非零（位压缩信号），"
                    f"暂不支持解码，已跳过"
                )
                ch_addr = next_ch
                continue
            byte_size = _WIDTH_TO_BYTES.get(bit_count)
            if byte_size is None:
                warnings.append(
                    f"通道 {name or ch_addr} 位宽 {bit_count} 不在 8/16/32/64 之列，"
                    f"暂不支持解码，已跳过"
                )
                ch_addr = next_ch
                continue
            if byte_offset + byte_size > samples_bytes:
                warnings.append(
                    f"通道 {name or ch_addr} 的字节偏移 {byte_offset} + "
                    f"{byte_size} 超出记录样本区 {samples_bytes}，已跳过"
                )
                ch_addr = next_ch
                continue

            out.append(
                {
                    "addr": ch_addr,
                    "name": name,
                    "unit": chain.text(unit_addr),
                    "spec": DataTypeSpec(
                        dtype=_SUPPORTED_DATA_TYPES[data_type],
                        bit_count=bit_count,
                        byte_order="little",
                    ),
                    "byte_offset": byte_offset,
                    "is_master": ch_type == _CH_MASTER,
                }
            )
            ch_addr = next_ch

        if not out:
            warnings.append(f"数据组 {dg_index} 通道组（偏移 {cg['addr']}）没有可用通道")
        return out

    # ------------------------------------------------------------------
    # unsorted（总线日志）
    # ------------------------------------------------------------------

    def _parse_unsorted(
        self,
        *,
        chain: _Chain,
        source: DataSource,
        dg_index: int,
        record_id_len: int,
        spans: list[tuple[int, int]],
        cgs: list[dict],
        measurement: Measurement,
        warnings: list[str],
        stats: dict[str, int],
    ) -> None:
        total_bytes = sum(length for _off, length in spans)
        usable = [cg for cg in cgs if not cg["skipped"]]
        for cg in usable:
            if cg["flags"] & _FLAG_CG_VLSD:
                warnings.append(
                    f"数据组 {dg_index} 通道组（偏移 {cg['addr']}）含 VLSD 变长通道，"
                    f"暂不支持，已跳过"
                )
                stats["skipped_groups"] += 1
                cg["skipped"] = True
            elif cg["flags"] & _FLAG_CG_REMOTE_MASTER:
                warnings.append(
                    f"数据组 {dg_index} 通道组（偏移 {cg['addr']}）是远程主通道组，"
                    f"暂不支持，已跳过"
                )
                stats["skipped_groups"] += 1
                cg["skipped"] = True
        usable = [cg for cg in cgs if not cg["skipped"]]
        if not usable:
            return

        strides = {cg["record_id"]: cg["samples_bytes"] + cg["inval_bytes"] for cg in usable}
        if len(set(strides.values())) > 1:
            warnings.append(
                f"数据组 {dg_index} 是未排序组，但各组记录步长不一致"
                f"（{sorted(set(strides.values()))} 字节）—— 顺序扫描无法可靠归属，"
                f"该组只建索引不取值"
            )
            self._index_unsorted_meta_only(
                chain, dg_index, usable, measurement, warnings, stats
            )
            return

        if total_bytes > _MAX_UNSORTED_BYTES:
            warnings.append(
                f"数据组 {dg_index} 数据区 {total_bytes / 1e6:.0f} MB 超过上限 "
                f"{_MAX_UNSORTED_BYTES / 1e6:.0f} MB，未排序组只建索引不取值"
            )
            self._index_unsorted_meta_only(
                chain, dg_index, usable, measurement, warnings, stats
            )
            return

        # 单次顺序扫描：按 record_id 归属，顺手抽出各组的样本区
        data = b"".join(source.read_at(off, length) for off, length in spans)
        rid_bytes = record_id_len
        buffers: dict[int, bytearray] = {cg["record_id"]: bytearray() for cg in usable}
        stride = next(iter(strides.values())) + rid_bytes
        pos = 0
        total_records = 0
        unknown_ids: set[int] = set()
        while pos + stride <= len(data):
            rid = int.from_bytes(data[pos : pos + rid_bytes], "little")
            buf = buffers.get(rid)
            if buf is None:
                unknown_ids.add(rid)
            else:
                buf += data[pos + rid_bytes : pos + stride]
            pos += stride
            total_records += 1
        if unknown_ids:
            sample = ", ".join(str(r) for r in sorted(unknown_ids)[:5])
            warnings.append(
                f"数据组 {dg_index} 有 {len(unknown_ids)} 个 record_id 没有对应的通道组"
                f"（如 {sample}），这些记录被忽略"
            )

        for cg in usable:
            count = len(buffers[cg["record_id"]]) // cg["samples_bytes"]
            if count != cg["cycles_nr"]:
                warnings.append(
                    f"未排序通道组（偏移 {cg['addr']}）扫描到 {count} 条属于自己的记录，"
                    f"块里声明 {cg['cycles_nr']} 条，以扫描结果为准"
                )
            self._index_cg_values(
                chain=chain,
                source=source,
                dg_index=dg_index,
                cg=cg,
                raw=bytes(buffers[cg["record_id"]]),
                stride=cg["samples_bytes"],
                count=count,
                measurement=measurement,
                warnings=warnings,
                stats=stats,
                note=f"未排序组（record_id_len={rid_bytes}），"
                f"取值在解析时一次性抽出；无效位一律按有效处理",
            )

    def _index_unsorted_meta_only(
        self,
        chain: _Chain,
        dg_index: int,
        cgs: list[dict],
        measurement: Measurement,
        warnings: list[str],
        stats: dict[str],
    ) -> None:
        for cg in cgs:
            self._index_cg_values(
                chain=chain,
                source=chain._source,
                dg_index=dg_index,
                cg=cg,
                raw=b"",
                stride=cg["samples_bytes"] + cg["inval_bytes"],
                count=0,
                measurement=measurement,
                warnings=warnings,
                stats=stats,
                note="未排序组且未取值（上限或不一致步长）",
            )

    def _index_cg_values(
        self,
        *,
        chain: _Chain,
        source: DataSource,
        dg_index: int,
        cg: dict,
        raw: bytes,
        stride: int,
        count: int,
        measurement: Measurement,
        warnings: list[str],
        stats: dict[str, int],
        note: str,
    ) -> None:
        """unsorted 组：样本已在内存（raw 为紧凑后的记录流），直接解码。"""
        channels = self._read_channels(chain, cg, warnings, dg_index)
        if not channels:
            stats["skipped_groups"] += 1
            return
        master: SignalSeries | None = None
        signals: list[SignalSeries] = []
        for ch in channels:
            series = SignalSeries(
                path=ch["name"] or f"ch{ch['addr']}",
                name=ch["name"],
                unit=ch["unit"],
                kind=SignalKind.CONTINUOUS,
                group=len(measurement.groups),
                spec=ch["spec"],
            )
            if raw:
                from automotive_dev_support.core.models.measurement.decode import (
                    decode_samples,
                )

                series.values = decode_samples(
                    raw, ch["spec"], count, stride, ch["byte_offset"]
                )
            if ch["is_master"] and master is None:
                master = series
            else:
                signals.append(series)
        if master is None and channels:
            warnings.append(
                f"数据组 {dg_index} 通道组（偏移 {cg['addr']}）没有时间主通道，"
                f"秒时间轴不可用"
            )
        measurement.groups.append(
            ChannelGroup(
                index=len(measurement.groups),
                name=cg["name"] or f"group {len(measurement.groups)}",
                master=master,
                signals=signals,
            )
        )
        stats["channel_groups"] += 1
        stats["signals"] += len(signals) + (1 if master else 0)
        measurement.meta[f"group_{len(measurement.groups) - 1}_note"] = note

    # ------------------------------------------------------------------
    # DT 链
    # ------------------------------------------------------------------

    def _dt_spans(
        self, chain: _Chain, data_block: int, warnings: list[str]
    ) -> list[tuple[int, int]]:
        """顺 DT 链收集数据段 (文件偏移, 字节数)。"""
        spans: list[tuple[int, int]] = []
        addr = data_block
        seen: set[int] = set()
        while addr:
            if addr in seen:
                warnings.append(f"数据块链在 {addr} 处成环，停止")
                break
            seen.add(addr)
            head = chain.block(addr, 32)
            if head is None:
                warnings.append(f"数据块地址 {addr} 越界，忽略后续数据块")
                break
            if head[:4] != _DT:
                warnings.append(
                    f"偏移 {addr} 处不是 ##DT 块（可能是 DZ 压缩块或 LD 块），"
                    f"暂不支持，该数据组无数据可取"
                )
                return []
            (block_len,) = _U64(head, _LEN_AT)
            (links_nr,) = _U64(head, _LINKS_NR_AT)
            data_len = block_len - 24
            if data_len < 0:
                warnings.append(f"偏移 {addr} 处 ##DT 块长度异常（{block_len}），忽略")
                break
            spans.append((addr + 24, data_len))
            # DT 块的第一个链接指向下一个 DT 块（数据跨块时）
            if links_nr >= 1:
                nxt = chain.block(addr, 32)
                if nxt is None:
                    break
                (addr,) = _U64(nxt, 24)
            else:
                addr = 0
        return spans
