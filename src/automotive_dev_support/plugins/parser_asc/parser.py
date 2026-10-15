"""Vector ASC 日志解析器插件。

把 `.asc` 文本日志解析成 `Measurement` 的**事件流**部分
（`events`，连续组为空）。ASC 是给人看的文本格式，
一行一帧，字段用空白分隔 —— 好处是坏了哪一行一眼看得出来，
所以本解析器的策略是**逐行独立判定，坏行跳过并计数**，
绝不因为一行可疑就连坐前后行。

## 时间基准（最容易踩的坑）

ASC 的时间戳有两层：

    文件头 `base hex timestamps absolute`  或  `relative`
    `Begin Triggerblock <日期> <时间>`      提供绝对锚点

- `relative`：行首数字就是从测量起点算起的秒数，直接用。
- `absolute`：行首数字要**加上锚点的 Unix 纪元秒**才是真实时刻。
  锚点优先取 Triggerblock 行，其次 date+time 行，都没有就退化为
  相对时间并告警 —— 猜一个锚点比没有锚点更糟。

`Event.t` 存的就是上述结果（绝对纪元秒或相对秒），
具体是哪种记在 `Measurement.meta["time_base"]` 里。

## 认识的行型

    <ts> <通道> <ID>[x] <Rx|Tx|TxRq> d <DLC> <数据字节...>   数据帧
    <ts> <通道> <ID>[x] <Rx|Tx> r <DLC>                      远程帧
    <ts> <通道> ErrorFrame                                   错误帧（无 ID 型）
    <ts> <通道> <ID>[x] <Rx|Tx> ErrorFrame                   错误帧（带 ID 型）
    <ts> CANFD <通道> <Rx|Tx> <ID> <符号名> <BRS> <ESI> <DLC> <长度> <数据...>

其余（注释、统计行、`Start of measurement`、J1939 行等）跳过并计数。
符号名为空时 FD 行会出现连续空格，因此 FD 行**从尾部向前匹配**
而不是按固定列号切 —— 按列号切会在无符号名的文件上整体错位。
"""

from __future__ import annotations

import re
from datetime import datetime

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

__all__ = ["AscParser"]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # CANoe 的德语本地化月名
    "mär": 3, "mai": 5, "okt": 10, "dez": 12,
}

_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun",
             "mo", "di", "mi", "do", "fr", "sa", "so"}

#: CANoe 各版本写过的日期时间形态：12/24 小时、年份在尾/在中、有无年份。
#: 按出现频率排序，先匹配先赢。
_DATETIME_FORMATS = (
    "%m %d %H:%M:%S.%f %Y",
    "%m %d %I:%M:%S.%f %p %Y",
    "%m %d %Y %I:%M:%S.%f %p",
    "%m %d %Y %H:%M:%S.%f",
    "%m %d %H:%M:%S %Y",
    "%m %d %I:%M:%S %p %Y",
    "%m %d %Y",
)

_RE_DATE = re.compile(r"^date\s+(?P<rest>.+)$", re.IGNORECASE)
_RE_TIME = re.compile(r"^time\s+(?P<rest>.+)$", re.IGNORECASE)
_RE_BASE = re.compile(
    r"^base\s+(?P<base>hex|dec)(?:\s+timestamps\s+(?P<ts>absolute|relative))?",
    re.IGNORECASE,
)
_RE_TRIGGER = re.compile(r"^begin\s+triggerblock\s+(?P<rest>.+)$", re.IGNORECASE)


def _normalize_datetime_text(text: str) -> str:
    """月名换成数字、去掉开头的星期名。"""
    tokens = text.split()
    if tokens and tokens[0].lower().rstrip(".") in _WEEKDAYS:
        tokens = tokens[1:]
    if tokens:
        head = tokens[0].lower().rstrip(".")
        if head in _MONTHS:
            tokens[0] = f"{_MONTHS[head]:02d}"
    return " ".join(tokens)


def _parse_canoe_datetime(text: str) -> float | None:
    """解析 CANoe 的日期时间串，返回 Unix 纪元秒（本地时区，格式不记时区）。

    形如 `Oct 15 2026`、`Thu Oct 15 13:41:21.123 2026`、
    `Oct 15 1:41:21.123 pm`。时区按运行环境本地时间解释 ——
    ASC 不记录时区，这是格式的固有缺陷，跨时区对照要在界面上提示。
    """
    normalized = _normalize_datetime_text(text)
    if not normalized:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).timestamp()
        except ValueError:
            continue
    return None


def _parse_time_only(text: str) -> tuple[int, int, int, int] | None:
    """解析 `1:41:21.123 pm` 或 `13:41:21.123`，返回 (h, m, s, us)。"""
    normalized = text.strip()
    for fmt in ("%I:%M:%S.%f %p", "%I:%M:%S %p", "%H:%M:%S.%f", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.hour, parsed.minute, parsed.second, parsed.microsecond
        except ValueError:
            continue
    return None


def _parse_id(token: str, base: int) -> tuple[int, bool]:
    """解析报文 ID。尾缀 `x` 表示扩展帧（CANoe 的写法）。"""
    if token[-1:].lower() == "x":
        return int(token[:-1], base), True
    return int(token, base), False


def _parse_fd_tail(tokens: list[str], base: int) -> tuple[dict, int] | None:
    """从尾部向前匹配 CAN FD 行的 `<BRS> <ESI> <DLC> <长度> <数据...>`。

    符号名可能为空（连续空格），所以不能按固定列号切。
    从尾部找：长度字段的值必须等于其后数据字节的个数，
    再向前校验 DLC/ESI/BRS 三个字段 —— 全部对得上才认。
    返回 (字段 dict, 消耗的 token 数)；认不出返回 None。
    """
    for data_len in range(min(65, len(tokens) - 4), -1, -1):
        len_idx = len(tokens) - 1 - data_len
        # len 字段之前至少要有 id + brs + esi + dlc 四个 token。
        # 注意是 continue 不是 break：data_len 越小 len_idx 越大，
        # 大的 data_len 布局不合法，小的可能合法。
        if len_idx < 4:
            continue
        try:
            declared = int(tokens[len_idx], 10)
        except ValueError:
            continue
        if declared != data_len:
            continue
        try:
            dlc = int(tokens[len_idx - 1], base)
            esi = int(tokens[len_idx - 2], 10)
            brs = int(tokens[len_idx - 3], 10)
        except ValueError:
            continue
        if esi not in (0, 1) or brs not in (0, 1):
            continue
        try:
            data = bytes(int(tok, base) for tok in tokens[len_idx + 1 :])
        except ValueError:
            continue
        consumed = len_idx + 1  # 到数据末尾
        return (
            {
                "brs": bool(brs),
                "esi": bool(esi),
                "dlc": dlc,
                "data": data,
            },
            consumed,
        )
    return None


class AscParser:
    """`core.ports.Parser` 的实现。产出事件流 Measurement。

    `max_events` 限制**帧体**驻留数量（统计永远全量）。
    ASC 是文本格式，一份几百 MB 的 asc 意味着上千万帧，
    全部实例化成 Event 对象会把内存吃光 —— 而总线负载、
    报文周期这类分析只看统计就够。超出上限的帧只进统计。
    """

    def __init__(self, max_events: int = 200_000) -> None:
        self.max_events = max_events

    kinds = frozenset({ArtifactKind.ASC})

    def parse(self, source: DataSource, ctx: JobContext) -> ParseOutcome:
        artifact = Artifact(
            kind=ArtifactKind.ASC,
            role=ArtifactRole.MEASUREMENT,
            ref=source.artifact_ref(),
            producer="parser-asc",
        )

        warnings: list[str] = []
        measurement = Measurement(label=_stem(source.uri))

        base = 16
        time_base = "relative"
        anchor: float | None = None
        date_parts: str | None = None
        time_part: str | None = None
        events: list[Event] = []
        stats: dict[tuple[int, int], EventStat] = {}
        skipped = 0
        frames = 0
        error_frames = 0
        remote_frames = 0
        fd_frames = 0

        for lineno, line in _iter_lines(source):
            stripped = line.strip()
            if not stripped:
                continue

            # -- 文件头 ------------------------------------------------------
            if m := _RE_BASE.match(stripped):
                base = 10 if m.group("base").lower() == "dec" else 16
                if m.group("ts"):
                    time_base = m.group("ts").lower()
                continue
            if m := _RE_DATE.match(stripped):
                date_parts = m.group("rest")
                continue
            if m := _RE_TIME.match(stripped):
                time_part = m.group("rest")
                continue
            if m := _RE_TRIGGER.match(stripped):
                anchor = _parse_canoe_datetime(m.group("rest"))
                continue
            if stripped.startswith("//") or stripped.lower().startswith(
                ("no internal events", "internal events")
            ):
                continue

            # -- 数据行 ------------------------------------------------------
            parsed = self._parse_line(stripped, base, warnings, lineno)
            if parsed is None:
                skipped += 1
                continue

            ts, ev = parsed
            if time_base == "absolute":
                # 锚点可能还没出现（极少数文件的 Triggerblock 在数据之后）
                if anchor is None:
                    anchor = self._anchor_from(date_parts, time_part)
                if anchor is None:
                    warnings.append(
                        "文件声明绝对时间戳但找不到起始锚点，已按相对时间解释"
                    )
                    ev.t = ts
                else:
                    ev.t = anchor + ts
            else:
                ev.t = ts
            if len(events) < self.max_events:
                events.append(ev)
            frames += 1
            key = (ev.channel, ev.frame_id)
            stat = stats.get(key)
            if stat is None:
                stats[key] = EventStat(
                    count=1, first_t=ev.t, last_t=ev.t, bytes=len(ev.data)
                )
            else:
                stat.count += 1
                stat.last_t = ev.t
                stat.bytes += len(ev.data)
            if ev.kind == "error":
                error_frames += 1
            elif ev.kind == "remote":
                remote_frames += 1
            if ev.is_fd:
                fd_frames += 1

        if skipped:
            warnings.append(f"跳过 {skipped} 个无法识别的行（注释/统计/损坏行），未计入事件")
        if len(events) < frames:
            warnings.append(
                f"帧体仅保留前 {len(events)} 帧（上限 {self.max_events}），"
                f"统计仍为全量 {frames} 帧"
            )

        measurement.events = events
        measurement.event_stats = stats
        measurement.meta = {
            "base": "hex" if base == 16 else "dec",
            "time_base": time_base,
            "anchor": anchor,
            "frames": frames,
            "error_frames": error_frames,
            "remote_frames": remote_frames,
            "fd_frames": fd_frames,
            "skipped_lines": skipped,
            "unique_ids": len(stats),
        }
        artifact.label = _stem(source.uri)
        return ParseOutcome(artifact=artifact, document=measurement, warnings=warnings)

    # -- 行解析 ---------------------------------------------------------------

    def _parse_line(
        self, line: str, base: int, warnings: list[str], lineno: int
    ) -> tuple[float, Event] | None:
        tokens = line.split()
        if len(tokens) < 2:
            return None
        try:
            ts = float(tokens[0])
        except ValueError:
            return None  # 不是数据行（统计行等），静默跳过由调用方计数

        # CAN FD：第二列固定是 CANFD 关键字
        if tokens[1].upper() == "CANFD":
            return self._parse_fd(tokens, base, warnings, lineno, ts, line)
        if not tokens[1].isdigit():
            return None
        channel = int(tokens[1])

        rest = tokens[2:]
        # 错误帧（无 ID 型）：通道号之后直接是 ErrorFrame
        if rest and rest[0].lower() == "errorframe":
            return ts, Event(t=0.0, channel=channel, kind="error")

        if len(rest) < 2:
            return None
        try:
            frame_id, extended = _parse_id(rest[0], base)
        except ValueError:
            warnings.append(f"第 {lineno} 行报文 ID 无法解析，已跳过：{line[:48]!r}")
            return None
        direction = rest[1].lower()
        tail = rest[2:]

        if tail and tail[0].lower() == "errorframe":
            # 错误帧（带 ID 型）
            return ts, Event(
                t=0.0, channel=channel, frame_id=frame_id,
                direction="rx" if direction == "rx" else "tx",
                kind="error", extended=extended,
            )

        if tail and tail[0].lower() == "r":
            dlc = 0
            if len(tail) > 1:
                try:
                    dlc = int(tail[1], base)
                except ValueError:
                    dlc = 0
            return ts, Event(
                t=0.0, channel=channel, frame_id=frame_id,
                direction="rx" if direction == "rx" else "tx",
                kind="remote", extended=extended, dlc=dlc,
            )

        if not tail or tail[0].lower() != "d":
            return None
        try:
            dlc = int(tail[1], base)
            data = bytes(int(tok, base) for tok in tail[2 : 2 + dlc])
        except ValueError:
            warnings.append(f"第 {lineno} 行数据字节无法解析，已跳过：{line[:48]!r}")
            return None
        if len(tail) < 2 + dlc:
            warnings.append(
                f"第 {lineno} 行声明 {dlc} 字节但只有 {max(0, len(tail) - 2)} 个，已跳过"
            )
            return None
        return ts, Event(
            t=0.0, channel=channel, frame_id=frame_id,
            direction="rx" if direction == "rx" else "tx",
            kind="can", extended=extended, dlc=dlc, data=data,
        )

    def _parse_fd(
        self,
        tokens: list[str],
        base: int,
        warnings: list[str],
        lineno: int,
        ts: float,
        line: str,
    ) -> tuple[float, Event] | None:
        # <ts> CANFD <通道> <方向> <ID> [符号名...] <BRS> <ESI> <DLC> <长度> <数据...>
        if len(tokens) < 6:
            return None
        try:
            channel = int(tokens[2])
        except ValueError:
            return None
        direction = tokens[3].lower()
        try:
            frame_id, extended = _parse_id(tokens[4], base)
        except ValueError:
            warnings.append(f"第 {lineno} 行 CAN FD 报文 ID 无法解析，已跳过")
            return None
        matched = _parse_fd_tail(tokens[4:], base)
        if matched is None:
            warnings.append(f"第 {lineno} 行 CAN FD 尾部字段无法匹配，已跳过：{line[:48]!r}")
            return None
        fields, _consumed = matched
        return ts, Event(
            t=0.0, channel=channel, frame_id=frame_id,
            direction="rx" if direction == "rx" else "tx",
            kind="can", extended=extended, is_fd=True,
            dlc=fields["dlc"], data=fields["data"],
        )

    @staticmethod
    def _anchor_from(date_parts: str | None, time_part: str | None) -> float | None:
        """date 行 + time 行拼出锚点。

        只有日期没有时间时**不返回**：拿午夜当锚点会把下午的记录
        整体挪走几个小时，比没有锚点更糟。那种情况交给调用方告警。
        """
        if date_parts and time_part:
            return _parse_canoe_datetime(f"{date_parts} {time_part}")
        return None


def _iter_lines(source: DataSource):
    """流式按行迭代。跨块的行在缓冲里拼接，不假设一行装得进一个块。"""
    buffer = b""
    lineno = 0
    for chunk in source.stream():
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for raw in lines:
            lineno += 1
            yield lineno, raw.decode("utf-8", errors="replace").rstrip("\r")
    if buffer:
        lineno += 1
        yield lineno, buffer.decode("utf-8", errors="replace")


def _stem(uri: str) -> str:
    tail = uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail
