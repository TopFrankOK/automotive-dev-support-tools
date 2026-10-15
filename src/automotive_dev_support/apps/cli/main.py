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

from pathlib import Path

import typer

from automotive_dev_support.core.models.measurement import SignalKind
from automotive_dev_support.core.ports import PluginRegistry
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
