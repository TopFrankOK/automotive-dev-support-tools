"""Vector BLF 二进制日志解析器插件。

BLF 是 Vector 的私有格式（无公开规范），本实现依据
Toby Lorenz 的开源 C++ 库 Vector BLF 与 python-can 的
Python 实现整理而来 —— 这两个实现对真实 CANoe 产物的
兼容性经过了长期检验。格式要点：

    文件头 144 字节（"LOGG" 签名，实际字段 72 字节，其余保留）
    其后是若干「日志容器」对象（"LOBJ" 头 + zlib 压缩/未压缩数据）
    容器内是具体对象：CAN 报文、CAN FD 报文、错误帧……
    每个对象后可能有 0-3 字节对齐填充

## 三个关键决策

1. **时间戳以文件头 SYSTEMTIME 为锚。** 对象时戳是相对测量的
    tick 数（1e-9 或 1e-5 秒/tick，由对象头 flags 指明），
   加上文件头起始时刻才是绝对时刻。文件头时间为空
   （记录崩溃未收尾）时退化为**相对首帧**并告警 ——
   此时绝对时刻已不可恢复，编一个是更坏的选择。

2. **填充靠「找下一个 LOBJ」消化，不假设对齐值。** 不同写入方
   （CANoe / python-can / 自研录波）的填充习惯不完全一致，
   硬按 `obj_size % 4` 跳会在某些文件上错位。向后找签名
   最多 8 字节，既兼容各种写法，也不会误吞数据。

3. **统计全量、帧体有界。** GB 级 BLF 有上亿帧，全部实例化
   Event 会爆内存。`max_events` 之外的帧只进统计。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from datetime import datetime, timezone

from automotive_dev_support.core.models.measurement import (
    Event,
    EventStat,
    Measurement,
)
from automotive_dev_support.core.ports.plugin import ParseOutcome
from automotive_dev_support.core.skeleton import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    DataSource,
    JobContext,
)

__all__ = ["BlfParser"]

# -- 格式常量（数值来自 Vector BLF / python-can 实现） ----------------------

#: 文件头：签名, 头长, 应用 ID + 7 个版本字节, 文件大小, 未压缩大小,
#: 对象数, 已读对象数, 起止 SYSTEMTIME（各 8×u2）。共 72 字节，补足 144。
FILE_HEADER = struct.Struct("<4sLBBBBBBBBQQLL8H8H")
FILE_HEADER_SIZE = 144

#: 对象公共头：签名, 头长, 头版本, 对象大小, 对象类型
OBJ_HEADER_BASE = struct.Struct("<4sHHLL")
#: 对象头 v1：时标 flags, 客户索引, 对象版本, 时戳
OBJ_HEADER_V1 = struct.Struct("<LHHQ")
#: 对象头 v2：时标 flags, 时标状态, 对象版本, 时戳（含尾填充）
OBJ_HEADER_V2 = struct.Struct("<LBxHQ8x")
#: 容器头：压缩方式, 未压缩大小（含填充字段）
LOG_CONTAINER = struct.Struct("<H6xL4x")
#: CAN 报文：通道, flags, DLC, ID, 8 字节数据
CAN_MSG = struct.Struct("<HBBL8s")
#: CAN FD 报文：通道, flags, DLC, ID, 帧长, 位计数, FD flags, 有效字节, 数据
CAN_FD_MSG = struct.Struct("<HBBLLBBB5x64s")
#: 扩展错误帧：通道, 长度, flags, ecc, 位置, DLC, 帧长, ID, 扩展 flags, 数据
CAN_ERROR_EXT = struct.Struct("<HHLBBBxLLH2x8s")

CAN_MESSAGE = 1
CAN_ERROR = 2
LOG_CONTAINER_TYPE = 10
CAN_ERROR_EXT_TYPE = 73
CAN_MESSAGE2 = 86
GLOBAL_MARKER = 96
CAN_FD_MESSAGE = 100
CAN_FD_MESSAGE_64 = 101
RESTORE_POINT_CONTAINER = 115

#: 对象头 flags：时标单位
TIME_TEN_MICS = 1
TIME_ONE_NANS = 2

CAN_MSG_EXT = 0x80000000
CAN_ID_MASK = 0x1FFFFFFF
REMOTE_FLAG = 0x80
DIR_FLAG = 0x01

NO_COMPRESSION = 0
ZLIB_DEFLATE = 2

_OBJECT_TYPE_NAMES = {
    CAN_MESSAGE: "CAN_MESSAGE",
    CAN_ERROR: "CAN_ERROR",
    LOG_CONTAINER_TYPE: "LOG_CONTAINER",
    CAN_ERROR_EXT_TYPE: "CAN_ERROR_EXT",
    CAN_MESSAGE2: "CAN_MESSAGE2",
    GLOBAL_MARKER: "GLOBAL_MARKER",
    CAN_FD_MESSAGE: "CAN_FD_MESSAGE",
    CAN_FD_MESSAGE_64: "CAN_FD_MESSAGE_64",
    RESTORE_POINT_CONTAINER: "RESTORE_POINT_CONTAINER",
}


class _ByteReader:
    """流式字节读取器：`read` / `peek`，不把整个文件拖进内存。"""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = bytearray()
        self._eof = False

    def _fill(self, size: int) -> None:
        while len(self._buf) < size and not self._eof:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._eof = True

    def read(self, size: int) -> bytes:
        self._fill(size)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def peek(self, size: int) -> bytes:
        self._fill(size)
        return bytes(self._buf[:size])

    def consume(self, size: int) -> None:
        del self._buf[:size]


class _RawStat:
    """统计的原始形态：时戳先记 tick 归一化后的纳秒整数，收尾再换算。"""

    __slots__ = ("count", "first_ns", "last_ns", "nbytes")

    def __init__(self, first_ns: int, nbytes: int) -> None:
        self.count = 1
        self.first_ns = first_ns
        self.last_ns = first_ns
        self.nbytes = nbytes


def _systemtime_epoch(fields: tuple[int, ...]) -> float | None:
    """SYSTEMTIME 八元组 → Unix 纪元秒（UTC）。

    BLF 的 SYSTEMTIME 不带时区，按 Vector 工具链的惯例解释为 UTC。
    全零（未收尾文件）或非法值返回 None。
    """
    year, month, _weekday, day, hour, minute, second, millis = fields
    try:
        moment = datetime(
            year, month, day, hour, minute, second, millis * 1000,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    return moment.timestamp()


class BlfParser:
    """`core.ports.Parser` 的实现。产出事件流 Measurement。

    `max_events` 限制**帧体**驻留数量（统计永远全量）。
    """

    def __init__(self, max_events: int = 200_000) -> None:
        self.max_events = max_events

    kinds = frozenset({ArtifactKind.BLF})

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        artifact = Artifact(
            kind=ArtifactKind.BLF,
            role=ArtifactRole.MEASUREMENT,
            ref=source.artifact_ref(),
            producer="parser-blf",
        )
        warnings: list[str] = []
        measurement = Measurement(label=_stem(source.uri))

        reader = _ByteReader(source.stream())
        # 注意读 144 而不是 struct 的 72：文件头有 72 字节保留区，
        # 不跳过它就会把填充当成对象区解析。
        raw_header = reader.read(FILE_HEADER_SIZE)
        if len(raw_header) < FILE_HEADER_SIZE or raw_header[:4] != b"LOGG":
            warnings.append(
                "不是 BLF 文件（文件头签名不是 'LOGG'，或文件不足 144 字节）"
            )
            measurement.meta = {"frames": 0, "finalized": False, "truncated": False}
            return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)

        fields = FILE_HEADER.unpack(raw_header[: FILE_HEADER.size])
        header_size = fields[1]
        if header_size != FILE_HEADER_SIZE:
            warnings.append(
                f"文件头声明 {header_size} 字节（标准为 144），按标准布局解析"
            )
        start_epoch = _systemtime_epoch(fields[14:22])
        stop_epoch = _systemtime_epoch(fields[22:30])
        finalized = start_epoch is not None
        if not finalized:
            warnings.append(
                "文件头起止时间为空（记录未正常收尾），事件时刻转为相对首帧，"
                "绝对时刻已不可恢复"
            )
        if start_epoch is not None:
            measurement.start_time = datetime.fromtimestamp(start_epoch, tz=timezone.utc)

        # -- 状态 ------------------------------------------------------------
        events: list[Event] = []
        pending_ns: list[int] = []  # 与 events 一一对应的原始纳秒（收尾换算用）
        stats: dict[tuple[int, int], _RawStat] = {}
        skipped: dict[str, int] = {}
        containers = 0
        frames = 0
        error_frames = 0
        fd_frames = 0
        first_ns: int | None = None
        truncated = False

        def emit(ns: int, ev: Event) -> None:
            nonlocal frames, error_frames, fd_frames, first_ns
            if first_ns is None:
                first_ns = ns
            if len(events) < self.max_events:
                events.append(ev)
                pending_ns.append(ns)
                if finalized:
                    ev.t = start_epoch + ns / 1e9
            key = (ev.channel, ev.frame_id)
            raw = stats.get(key)
            if raw is None:
                stats[key] = _RawStat(ns, len(ev.data))
            else:
                raw.count += 1
                raw.last_ns = ns
                raw.nbytes += len(ev.data)
            frames += 1
            if ev.kind == "error":
                error_frames += 1
            if ev.is_fd:
                fd_frames += 1

        # -- 顶层对象循环 ------------------------------------------------------
        while True:
            head = reader.peek(OBJ_HEADER_BASE.size)
            if len(head) < 4:
                break  # 干净的文件尾
            if head[:4] != b"LOBJ":
                # 容忍对齐填充 / 少量垃圾：有界地找下一个对象头
                window = reader.peek(24)
                idx = window.find(b"LOBJ")
                if idx < 0 or idx > 8:
                    warnings.append(
                        "偏移处既不是对象头也不像对齐填充，停止解析"
                        f"（已解析 {containers} 个容器 / {frames} 帧）"
                    )
                    truncated = True
                    break
                reader.consume(idx)
                head = reader.peek(OBJ_HEADER_BASE.size)
            if len(head) < OBJ_HEADER_BASE.size:
                warnings.append("文件末尾的对象头不完整（文件可能被截断）")
                truncated = True
                break
            # 把头消费掉再读体 —— peek 只是看，不消耗
            reader.consume(OBJ_HEADER_BASE.size)
            _sig, _hs, _hv, obj_size, obj_type = OBJ_HEADER_BASE.unpack(head)
            if obj_size < OBJ_HEADER_BASE.size:
                warnings.append(f"对象大小 {obj_size} 小于对象头，停止解析")
                truncated = True
                break
            body = reader.read(obj_size - OBJ_HEADER_BASE.size)
            if len(body) < obj_size - OBJ_HEADER_BASE.size:
                # 读不够就是 EOF（文件被截断）。容器尽力解析已有部分：
                # 未压缩容器里排在前面的事件是完整可用的，
                # 压缩容器交给 zlib 能解多少解多少。
                warnings.append("文件末尾的对象不完整（文件可能被截断），尽力解析已有部分")
                truncated = True
                if not body:
                    break
            obj = head + body

            if obj_type == LOG_CONTAINER_TYPE:
                containers += 1
                self._parse_container(obj, warnings, skipped, emit)
            elif obj_type in (CAN_MESSAGE, CAN_MESSAGE2, CAN_FD_MESSAGE, CAN_ERROR_EXT_TYPE):
                self._parse_object(obj, warnings, skipped, emit)
            else:
                name = _OBJECT_TYPE_NAMES.get(obj_type, str(obj_type))
                skipped[name] = skipped.get(name, 0) + 1

        # -- 收尾：时刻换算 ----------------------------------------------------
        if not finalized and first_ns is not None:
            origin = first_ns
            for ev, ns in zip(events, pending_ns):
                ev.t = (ns - origin) / 1e9

        event_stats = {
            key: EventStat(
                count=raw.count,
                first_t=(start_epoch + raw.first_ns / 1e9)
                if finalized
                else (raw.first_ns - (first_ns or 0)) / 1e9,
                last_t=(start_epoch + raw.last_ns / 1e9)
                if finalized
                else (raw.last_ns - (first_ns or 0)) / 1e9,
                bytes=raw.nbytes,
            )
            for key, raw in stats.items()
        }

        if len(events) < frames:
            warnings.append(
                f"帧体仅保留前 {len(events)} 帧（上限 {self.max_events}），"
                f"统计仍为全量 {frames} 帧"
            )
        if skipped:
            detail = "、".join(f"{name}×{count}" for name, count in sorted(skipped.items()))
            warnings.append(f"跳过 {sum(skipped.values())} 个不支持的对象：{detail}")

        measurement.events = events
        measurement.event_stats = event_stats
        measurement.meta = {
            "file_size": fields[10],
            "uncompressed_size": fields[11],
            "object_count": fields[12],
            "application_id": fields[2],
            "containers": containers,
            "frames": frames,
            "error_frames": error_frames,
            "fd_frames": fd_frames,
            "unique_ids": len(stats),
            "time_base": "absolute" if finalized else "relative",
            "anchor": start_epoch,
            "finalized": finalized,
            "truncated": truncated,
            "skipped_objects": dict(skipped),
        }
        artifact.label = _stem(source.uri)
        return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)

    # -- 容器 -----------------------------------------------------------------

    def _parse_container(
        self,
        obj: bytes,
        warnings: list[str],
        skipped: dict[str, int],
        emit,
    ) -> None:
        if len(obj) < OBJ_HEADER_BASE.size + LOG_CONTAINER.size:
            warnings.append("日志容器头不完整，跳过该容器")
            return
        method, _size_uncompressed = LOG_CONTAINER.unpack_from(
            obj, OBJ_HEADER_BASE.size
        )
        payload = obj[OBJ_HEADER_BASE.size + LOG_CONTAINER.size :]
        if method == NO_COMPRESSION:
            data = payload
        elif method == ZLIB_DEFLATE:
            try:
                data = zlib.decompressobj().decompress(payload)
            except zlib.error as exc:
                warnings.append(f"容器 zlib 解压失败（{exc}），跳过该容器")
                return
        else:
            warnings.append(f"不支持的容器压缩方式 {method}，跳过该容器")
            return
        self._parse_objects(data, warnings, skipped, emit)

    def _parse_objects(
        self,
        data: bytes,
        warnings: list[str],
        skipped: dict[str, int],
        emit,
    ) -> None:
        pos = 0
        end = len(data)
        base_size = OBJ_HEADER_BASE.size
        while pos + base_size <= end:
            sig, _hs, _hv, obj_size, obj_type = OBJ_HEADER_BASE.unpack_from(data, pos)
            if sig != b"LOBJ":
                idx = data.find(b"LOBJ", pos, min(pos + 8, end))
                if idx < 0:
                    warnings.append("容器内找不到下一个对象头，放弃容器剩余数据")
                    return
                pos = idx
                continue
            if obj_size < base_size or pos + obj_size > end:
                warnings.append("容器内对象大小越界，放弃容器剩余数据")
                return
            obj = data[pos : pos + obj_size]
            pos += obj_size
            # 消化对齐填充：下一个对象未必紧跟（各写入方习惯不同）
            if data[pos : pos + 4] != b"LOBJ":
                idx = data.find(b"LOBJ", pos, min(pos + 8, end))
                if idx >= 0:
                    pos = idx
            if obj_type in (CAN_MESSAGE, CAN_MESSAGE2, CAN_FD_MESSAGE, CAN_ERROR_EXT_TYPE):
                self._parse_object(obj, warnings, skipped, emit)
            else:
                name = _OBJECT_TYPE_NAMES.get(obj_type, str(obj_type))
                skipped[name] = skipped.get(name, 0) + 1

    # -- 单个对象 ---------------------------------------------------------------

    def _parse_object(
        self,
        obj: bytes,
        warnings: list[str],
        skipped: dict[str, int],
        emit,
    ) -> None:
        _sig, _hs, header_version, obj_size, obj_type = OBJ_HEADER_BASE.unpack_from(obj)
        if header_version == 1:
            time_flags, _client, _version, ticks = OBJ_HEADER_V1.unpack_from(obj, 16)
            payload_at = 16 + OBJ_HEADER_V1.size
        elif header_version == 2:
            time_flags, _status, _version, ticks = OBJ_HEADER_V2.unpack_from(obj, 16)
            payload_at = 16 + OBJ_HEADER_V2.size
        else:
            name = _OBJECT_TYPE_NAMES.get(obj_type, str(obj_type))
            skipped[f"{name}(头版本{header_version})"] = (
                skipped.get(f"{name}(头版本{header_version})", 0) + 1
            )
            return

        # 时标归一成纳秒整数：1e-5 秒/tick → ×1e4；1e-9 → ×1。全程整数运算。
        ns = ticks if time_flags == TIME_ONE_NANS else ticks * 10_000

        ev: Event | None = None
        if obj_type in (CAN_MESSAGE, CAN_MESSAGE2):
            if len(obj) < payload_at + CAN_MSG.size:
                warnings.append("CAN 报文对象长度不足，跳过")
                return
            channel, msg_flags, dlc, can_id, data = CAN_MSG.unpack_from(obj, payload_at)
            is_remote = bool(msg_flags & REMOTE_FLAG)
            ev = Event(
                t=0.0,
                channel=channel,
                frame_id=can_id & CAN_ID_MASK,
                dlc=dlc,
                # 远程帧的 8 字节区放的是请求长度不是载荷，不能当成数据
                data=b"" if is_remote else data[:dlc],
                direction="tx" if msg_flags & DIR_FLAG else "rx",
                kind="remote" if is_remote else "can",
                extended=bool(can_id & CAN_MSG_EXT),
            )
        elif obj_type == CAN_FD_MESSAGE:
            if len(obj) < payload_at + CAN_FD_MSG.size:
                warnings.append("CAN FD 报文对象长度不足，跳过")
                return
            (
                channel,
                msg_flags,
                dlc,
                can_id,
                _frame_length,
                _bit_count,
                fd_flags,
                valid_bytes,
                data,
            ) = CAN_FD_MSG.unpack_from(obj, payload_at)
            is_remote = bool(msg_flags & REMOTE_FLAG)
            ev = Event(
                t=0.0,
                channel=channel,
                frame_id=can_id & CAN_ID_MASK,
                dlc=dlc,
                data=b"" if is_remote else data[:valid_bytes],
                direction="tx" if msg_flags & DIR_FLAG else "rx",
                kind="remote" if is_remote else "can",
                extended=bool(can_id & CAN_MSG_EXT),
                is_fd=True,
            )
        elif obj_type == CAN_ERROR_EXT_TYPE:
            if len(obj) < payload_at + CAN_ERROR_EXT.size:
                warnings.append("错误帧对象长度不足，跳过")
                return
            (
                channel,
                _length,
                _err_flags,
                _ecc,
                _position,
                dlc,
                _frame_length,
                can_id,
                _flags_ext,
                data,
            ) = CAN_ERROR_EXT.unpack_from(obj, payload_at)
            ev = Event(
                t=0.0,
                channel=channel,
                frame_id=can_id & CAN_ID_MASK,
                dlc=dlc,
                data=data[:dlc],
                direction="rx",
                kind="error",
                extended=bool(can_id & CAN_MSG_EXT),
            )

        if ev is not None:
            emit(ns, ev)


def _stem(uri: str) -> str:
    tail = uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail
