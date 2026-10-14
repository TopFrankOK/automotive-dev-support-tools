"""命令行入口。

刻意保持薄：所有用例逻辑在 `services/`，这里只做参数解析与展示。
桌面壳（Tauri）将来通过 FastAPI 服务走同一条 services 路。

    ads compare --baseline-a2l old.a2l --baseline-image old.hex \
                --candidate-a2l new.a2l --candidate-image new.hex \
                --out report.xlsx
"""

from __future__ import annotations

from pathlib import Path

import typer

from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.services import CalibrationSources, run_compare

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
