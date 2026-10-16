"""命令行入口。

刻意保持薄：所有用例逻辑在 `services/`，这里只做参数解析与展示。
桌面壳（Tauri）将来通过 FastAPI 服务走同一条 services 路。

    ads compare --baseline-a2l old.a2l --baseline-image old.hex \
                --candidate-a2l new.a2l --candidate-image new.hex \
                --out report.xlsx

    ads signals measurement.mf4
    ads trend   measurement.mf4 --signal EngineSpeed --points 40
    ads align   old.mf4 new.mf4 --signal EngineSpeed --step 0.01
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from automotive_dev_support.core.models.common import sha256_hex
from automotive_dev_support.core.models.flash import FlashTarget
from automotive_dev_support.core.models.measurement import SignalKind
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton.event import EventBus, Topics
from automotive_dev_support.core.skeleton.job import JobContext, JobState
from automotive_dev_support.platform.flash import AuditLog, CheckpointStore
from automotive_dev_support.platform.licensing.config import default_data_dir
from automotive_dev_support.platform.store.filesource import FileSource, kind_from_suffix
from automotive_dev_support.services.flash import resume_flash
from automotive_dev_support.services import (
    CalibrationSources,
    align_signal,
    extract_trend,
    format_event_key,
    load_measurement,
    run_compare,
)

app = typer.Typer(
    name="ads",
    help="Automotive Software Development Support Tools —— 汽车嵌入式研发办公助手",
    no_args_is_help=True,
    add_completion=False,
)

#: 插件目录。相对本文件定位，不依赖运行时工作目录。
PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"


def _registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.load_all([PLUGINS_DIR])
    return registry


def _load(path: Path, what: str):
    if not path.exists():
        typer.echo(f"错误：{what}不存在：{path}", err=True)
        raise typer.Exit(2)
    try:
        return load_measurement(_registry(), path)
    except (ValueError, TypeError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def compare(
    baseline_a2l: Path = typer.Option(..., "--baseline-a2l", help="基准版 A2L 描述文件"),
    candidate_a2l: Path = typer.Option(..., "--candidate-a2l", help="对比版 A2L 描述文件"),
    baseline_values: Path | None = typer.Option(
        None, "--baseline-values", help="基准版值集（CDFX）—— 与 --baseline-image 二选一"
    ),
    candidate_values: Path | None = typer.Option(
        None, "--candidate-values", help="对比版值集（CDFX）—— 与 --candidate-image 二选一"
    ),
    baseline_image: Path | None = typer.Option(
        None, "--baseline-image", help="基准版内存镜像（HEX / S19）—— 与 --baseline-values 二选一"
    ),
    candidate_image: Path | None = typer.Option(
        None, "--candidate-image", help="对比版内存镜像（HEX / S19）—— 与 --candidate-values 二选一"
    ),
    out: Path | None = typer.Option(None, "--out", "-o", help="Excel 报告输出路径（.xlsx）"),
    byte_order: str = typer.Option(
        "big", "--byte-order", help="镜像字节序：big（默认，绝大多数 ECU）或 little"
    ),
    tolerance: float = typer.Option(0.0, "--tolerance", help="数值比对容差，默认严格相等"),
) -> None:
    """比对两个版本的标定数据（A2L + 值集/镜像 → 差异 → 报告）。"""
    for label, path in [
        ("基准版 A2L", baseline_a2l),
        ("对比版 A2L", candidate_a2l),
        ("基准版值集", baseline_values),
        ("对比版值集", candidate_values),
        ("基准版镜像", baseline_image),
        ("对比版镜像", candidate_image),
    ]:
        if path is not None and not path.exists():
            typer.echo(f"错误：{label}不存在：{path}", err=True)
            raise typer.Exit(2)

    baseline = CalibrationSources(
        a2l=baseline_a2l,
        values=baseline_values,
        image=baseline_image,
        label=baseline_a2l.name,
        byte_order=byte_order,
    )
    candidate = CalibrationSources(
        a2l=candidate_a2l,
        values=candidate_values,
        image=candidate_image,
        label=candidate_a2l.name,
        byte_order=byte_order,
    )

    outcome = run_compare(
        _registry(),
        baseline,
        candidate,
        out=out,
        params={"tolerance": tolerance},
    )

    for warning in (*outcome.baseline.warnings, *outcome.candidate.warnings):
        typer.echo(f"⚠ {warning}", err=True)

    cs = outcome.changeset
    s = cs.summary()
    typer.echo("")
    typer.echo(f"基准版本：{cs.baseline_label}")
    typer.echo(f"对比版本：{cs.candidate_label}")
    typer.echo(
        f"新增 {s['added']} / 删除 {s['removed']} / 改动 {s['changed']}"
        f"（其中纯地址漂移 {s['address_moved']}）"
        f" → 需要关注 {s['meaningful']} 项"
    )

    for change in cs.meaningful_changes():
        kinds = "、".join(str(k.value) for k in change.kinds)
        typer.echo(f"  [{kinds}] {change.path}")

    if outcome.report_path is not None:
        typer.echo(f"\n报告已写入：{outcome.report_path}")


@app.command()
def signals(file: Path = typer.Argument(..., help="测量文件（MF4 / BLF / ASC）")) -> None:
    """列出测量文件里的信号（连续通道组 + 事件流报文 ID）。"""
    loaded = _load(file, "测量文件")
    for warning in loaded.warnings:
        typer.echo(f"⚠ {warning}", err=True)
    m = loaded.document

    typer.echo(f"文件：{file.name}")
    if m.start_time is not None:
        typer.echo(f"起始：{m.start_time.isoformat()}")
    if m.groups:
        typer.echo(f"\n连续通道组（{len(m.groups)} 组）：")
        for grp in m.groups:
            master = grp.master.name if grp.master else "（无主通道）"
            typer.echo(f"  组 {grp.index}：{grp.name or '—'}  主通道 {master}")
            for sig in grp.signals:
                unit = f" [{sig.unit}]" if sig.unit else ""
                loaded_mark = "已载入" if sig.loaded else "懒加载"
                typer.echo(
                    f"    {sig.name or sig.path:<24}{unit:<10}"
                    f"{sig.sample_count} 样本（{loaded_mark}）"
                )
    if m.events or m.event_stats:
        counts = m.frame_counts()
        typer.echo(f"\n事件流（{len(counts)} 个报文 ID，共 {sum(counts.values())} 帧）：")
        for (channel, frame_id), count in sorted(counts.items()):
            typer.echo(f"    {format_event_key(channel, frame_id):<16}{count} 帧")


@app.command()
def trend(
    file: Path = typer.Argument(..., help="测量文件（MF4 / BLF / ASC）"),
    signal: str = typer.Option(..., "--signal", "-s", help="信号名或事件键 ch1/0x850"),
    points: int = typer.Option(2000, "--points", "-n", help="抽稀目标点数（min/max 带数）"),
    start: float | None = typer.Option(None, "--from", help="时间窗起点（相对秒）"),
    end: float | None = typer.Option(None, "--to", help="时间窗终点（相对秒）"),
    show: int = typer.Option(8, "--show", help="打印前多少条抽稀结果"),
) -> None:
    """抽取一条信号的趋势（min/max 抽稀，尖峰不会因抽样丢失）。"""
    loaded = _load(file, "测量文件")
    for warning in loaded.warnings:
        typer.echo(f"⚠ {warning}", err=True)

    window = None if start is None and end is None else (start or 0.0, end or 0.0)
    try:
        result = extract_trend(
            loaded, signal, max_points=points, window=window
        )
    except (KeyError, ValueError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(2) from exc

    unit = f" [{result.unit}]" if result.unit else ""
    typer.echo(f"信号：{result.signal}{unit}（{result.kind.value}，{result.sample_count} 样本）")
    if result.window is not None:
        base = "相对首样本" if result.time_base == "relative" else "Unix 纪元秒（绝对时刻）"
        typer.echo(
            f"时间窗：{result.window[0]:.3f} – {result.window[1]:.3f} s（{base}）"
        )
    typer.echo(f"抽稀：{result.note}")

    if result.bands:
        for band in result.bands[:show]:
            typer.echo(
                f"  {band.t_start:8.3f}–{band.t_end:8.3f} s  "
                f"{band.v_min:12.4f} … {band.v_max:12.4f}  ({band.count} 样本)"
            )
        if len(result.bands) > show:
            typer.echo(f"  ……其余 {len(result.bands) - show} 带略")
    if result.times is not None:
        for t, v in list(zip(result.times, result.values))[:show]:
            typer.echo(f"  {t:8.3f} s  {v:12.4f}")
        if len(result.times) > show:
            typer.echo(f"  ……其余 {len(result.times) - show} 桶略")

    if result.stats:
        pretty = "  ".join(f"{k}={v:.4f}" for k, v in result.stats.items())
        typer.echo(f"概览：{pretty}")


@app.command()
def align(
    file_a: Path = typer.Argument(..., help="第一次测量文件"),
    file_b: Path = typer.Argument(..., help="第二次测量文件"),
    signal: str = typer.Option(..., "--signal", "-s", help="要对齐的信号名"),
    step: float = typer.Option(0.01, "--step", help="公共栅格步长（秒）"),
    start: float | None = typer.Option(None, "--from", help="只对齐这个起点之后（相对秒）"),
    end: float | None = typer.Option(None, "--to", help="只对齐到这个终点之前（相对秒）"),
) -> None:
    """把两次测量里的同一条信号对齐到公共栅格，报告差异。

    只对连续信号有意义。时间范围不交集时直接报错 ——
    不重叠的两次测量，「对齐后比较」的结论必然是假的。
    """
    loaded_a = _load(file_a, "第一次测量文件")
    loaded_b = _load(file_b, "第二次测量文件")
    window = None if start is None and end is None else (start or 0.0, end or 0.0)
    try:
        result = align_signal(loaded_a, loaded_b, signal, step=step, window=window)
    except (KeyError, ValueError) as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise typer.Exit(2) from exc

    typer.echo(f"信号：{result.signal}（{result.note}）")
    typer.echo(f"单位：A=[{result.unit_a}]  B=[{result.unit_b}]")
    typer.echo(f"栅格：{len(result.grid)} 点，步长 {result.step} s")
    typer.echo(f"差异：最大 {result.diff_max:.6f}，RMS {result.diff_rms:.6f}")


@app.command()
def plugins() -> None:
    """列出已发现的插件（排查「功能怎么没出现」用）。"""
    registry = _registry()
    summary = registry.summary()
    typer.echo(f"共发现 {summary['total']} 个插件，可用 {summary['usable']} 个\n")
    for plugin in registry:
        caps = "、".join(str(c) for c in plugin.manifest.capabilities)
        state = "可用" if plugin.usable else f"不可用：{plugin.error}"
        typer.echo(f"  {plugin.manifest.id:<20} [{caps}] {state}")
    if summary["broken"]:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# 刷写：在线、有状态、不可重放
# ---------------------------------------------------------------------------

flash_app = typer.Typer(
    help="软件刷写 —— 在线、有状态、不可重放（目前只接仿真网关）",
    no_args_is_help=True,
)
app.add_typer(flash_app, name="flash")


def _flash_state_dir() -> Path:
    """刷写状态目录：检查点与审计记录都放这里，长期留存。

    可用环境变量 `ADS_FLASH_STATE_DIR` 覆盖（便于测试与自定义存放位置）。
    """
    override = os.environ.get("ADS_FLASH_STATE_DIR")
    if override:
        return Path(override)
    return default_data_dir() / "flash"


def _need_plugin(registry: PluginRegistry, plugin_id: str, what: str):
    plugin = registry.get(plugin_id)
    if plugin is None:
        typer.echo(f"错误：没有找到{what}插件：{plugin_id}", err=True)
        raise typer.Exit(2)
    if not plugin.usable:
        typer.echo(f"错误：{what}插件不可用：{plugin.error}", err=True)
        raise typer.Exit(2)
    return plugin.instance


def _load_image(registry: PluginRegistry, path: Path):
    parsers = registry.parsers_for(kind_from_suffix(path))
    if not parsers:
        typer.echo(f"错误：没有能解析该格式的插件：{path.suffix}（{path}）", err=True)
        raise typer.Exit(2)
    outcome = parsers[0].instance.parse(
        FileSource(path), JobContext(job_id="load-image")
    )
    document = outcome.document
    if document is None or not getattr(document, "segments", None):
        typer.echo(f"错误：镜像里没有可读的段：{path}", err=True)
        raise typer.Exit(2)
    for warning in outcome.warnings:
        typer.echo(f"警告：{warning}", err=True)
    return document


def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def _do_flash(
    *,
    image: Path,
    target_id: str,
    target_name: str,
    channel: str,
    operator: str,
    recipe_id: str,
    gateway_id: str,
    block_size: int,
    require_checkpoint: bool,
) -> None:
    if not image.exists():
        typer.echo(f"错误：镜像不存在：{image}", err=True)
        raise typer.Exit(2)

    registry = _registry()
    recipe = _need_plugin(registry, recipe_id, "配方")
    gateway = _need_plugin(registry, gateway_id, "网关")
    space = _load_image(registry, image)
    segments = list(space.segments)

    state_dir = _flash_state_dir()
    store = CheckpointStore(state_dir / f"checkpoint-{_safe_name(target_id)}.json")
    audit = AuditLog(state_dir / "audit.jsonl")

    if require_checkpoint and not store.exists():
        typer.echo(
            f"错误：目标 {target_id} 没有可续的检查点，请先执行一次 flash run",
            err=True,
        )
        raise typer.Exit(2)

    target = FlashTarget(id=target_id, name=target_name, channel=channel)
    fingerprint = sha256_hex(b"".join(s.data for s in segments))

    bus = EventBus()
    bus.subscribe(
        Topics.FLASH_STEP,
        lambda e: typer.echo(
            f"  [{int(e.payload['index']) + 1}] {e.payload['label']}"
        ),
    )
    bus.subscribe(
        Topics.JOB_PROGRESS,
        lambda e: typer.echo(
            f"      {e.payload['done']}/{e.payload['total']} 字节"
        ),
    )
    ctx = JobContext(job_id=f"flash-{_safe_name(target_id)}", bus=bus)

    result = resume_flash(
        recipe=recipe,
        gateway=gateway,
        target=target,
        segments=segments,
        image_fingerprint=fingerprint,
        ctx=ctx,
        checkpoint_store=store,
        audit_log=audit,
        operator=operator,
        block_size=block_size,
    )

    if result.state is JobState.SUCCEEDED:
        resumed = result.payload.get("resumed_from")
        typer.echo(
            f"刷写完成：{len(segments)} 段 / "
            f"{result.payload.get('bytes_done', 0)} 字节 → {target_id}"
        )
        if resumed:
            typer.echo(f"（从第 {resumed} 段续刷）")
        return

    if result.state is JobState.CANCELLED:
        typer.echo("刷写已取消；已完成的部分已记录，可用 flash resume 继续", err=True)
        raise typer.Exit(1)

    error = result.error or "未知原因"
    # 「拒绝续刷」属于状态不符而不是刷写失败，退出码与失败区分开
    code = 2 if "拒绝续刷" in error else 1
    typer.echo(f"刷写失败：{error}", err=True)
    raise typer.Exit(code)


@flash_app.command("run")
def flash_run(
    image: Path = typer.Argument(..., help="镜像文件（HEX / S19）"),
    target: str = typer.Option(..., "--target", "-t", help="目标标识，例如 ECM-01"),
    target_name: str = typer.Option("", "--target-name", help="目标名称（可选）"),
    channel: str = typer.Option(
        "sim://default", "--channel", help="网关通道（原样传给网关）"
    ),
    operator: str = typer.Option("", "--operator", help="操作者（进审计记录）"),
    recipe: str = typer.Option("flash-recipe-sim", "--recipe", help="配方插件 id"),
    gateway: str = typer.Option("gateway-sim", "--gateway", help="网关插件 id"),
    block_size: int = typer.Option(32, "--block-size", help="传输分块大小（字节）"),
) -> None:
    """把镜像刷进目标。存在可续的检查点时自动从中断处继续。"""
    _do_flash(
        image=image,
        target_id=target,
        target_name=target_name,
        channel=channel,
        operator=operator,
        recipe_id=recipe,
        gateway_id=gateway,
        block_size=block_size,
        require_checkpoint=False,
    )


@flash_app.command("resume")
def flash_resume(
    image: Path = typer.Argument(..., help="镜像文件（必须与上次同一份）"),
    target: str = typer.Option(..., "--target", "-t", help="目标标识"),
    target_name: str = typer.Option("", "--target-name", help="目标名称（可选）"),
    channel: str = typer.Option("sim://default", "--channel", help="网关通道"),
    operator: str = typer.Option("", "--operator", help="操作者（进审计记录）"),
    recipe: str = typer.Option("flash-recipe-sim", "--recipe", help="配方插件 id"),
    gateway: str = typer.Option("gateway-sim", "--gateway", help="网关插件 id"),
    block_size: int = typer.Option(32, "--block-size", help="传输分块大小（字节）"),
) -> None:
    """从上次中断处继续刷。没有可续的检查点则报错（退出码 2）。"""
    _do_flash(
        image=image,
        target_id=target,
        target_name=target_name,
        channel=channel,
        operator=operator,
        recipe_id=recipe,
        gateway_id=gateway,
        block_size=block_size,
        require_checkpoint=True,
    )


@flash_app.command("history")
def flash_history(
    target: str | None = typer.Option(None, "--target", "-t", help="按目标标识过滤"),
    fingerprint: str | None = typer.Option(
        None, "--fingerprint", help="按镜像指纹过滤（回答「这个版本刷过哪些台」）"
    ),
    limit: int = typer.Option(20, "--limit", help="最多显示几条（按时间倒序）"),
) -> None:
    """列出历史刷写记录（成功与失败都留痕）。"""
    audit = AuditLog(_flash_state_dir() / "audit.jsonl")
    records = audit.all()
    if target:
        records = [r for r in records if r.target_id == target]
    if fingerprint:
        records = [r for r in records if r.image_fingerprint == fingerprint]

    if not records:
        typer.echo("没有记录。")
        return

    typer.echo(f"共 {len(records)} 条记录（显示最近 {limit} 条）\n")
    for record in records[-limit:]:
        state = "成功" if record.success else f"失败：{record.error or '未知原因'}"
        typer.echo(
            f"  {record.started_at:%Y-%m-%d %H:%M:%S}  "
            f"{record.target_id:<12} {record.image_fingerprint}  {state}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
