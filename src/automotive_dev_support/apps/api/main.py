"""HTTP 服务层：把 services 暴露成 REST，并托管前端页面。

## 为什么单独一个 app 而不是塞进 CLI

CLI 的输入是命令行、输出是文本；这里的输入是 JSON、输出是 JSON。
两者的**参数解析与展示**完全不同，但调用的 service 是同一个 ——
所以按 `apps/` 下一入口一个文件分开，谁也不羡慕谁。

## 安全边界

只监听 `127.0.0.1`：没有鉴权，不对局域网开放。
文件浏览（后续任务）限制在配置根目录下，不暴露任意路径。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from automotive_dev_support.core.models.common import sha256_hex
from automotive_dev_support.core.models.flash import FlashTarget
from automotive_dev_support.core.models.memory import AddressSpace
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import ArtifactKind, JobContext
from automotive_dev_support.core.skeleton.event import EventBus, Topics
from automotive_dev_support.core.skeleton.job import JobState
from automotive_dev_support.platform.flash import AuditLog, CheckpointStore
from automotive_dev_support.platform.licensing.config import default_data_dir
from automotive_dev_support.platform.store import FileSource, kind_from_suffix
from automotive_dev_support.services import (
    CalibrationSources,
    extract_trend,
    format_event_key,
    load_measurement,
    run_compare,
)
from automotive_dev_support.services.flash import resume_flash
from automotive_dev_support.services.writeback import (
    WritebackError,
    WritebackSources,
    run_writeback,
)

#: 前端静态文件。相对本文件定位，不依赖运行时工作目录。
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 插件目录。与 CLI 用同一个（`Path(__file__).parents[2]` = `automotive_dev_support`）。
PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"

app = FastAPI(
    title="汽车嵌入式研发助手",
    version="0.1.0",
    description="标定比对 / 测量数据 / 写回 / 刷写 —— 与 CLI 共用同一套 services",
)


def _registry() -> PluginRegistry:
    """现加载一个注册表。

    不缓存：插件目录下的代码可能正在改（这个项目就是），
    每次请求重新加载才不会出现「改了代码但界面还是旧的」。
    """
    registry = PluginRegistry()
    registry.load_all([PLUGINS_DIR])
    return registry


@app.get("/api/health")
def health() -> dict[str, object]:
    """探活。前端用它判断服务起没起。"""
    return {"ok": True, "plugins": len(_registry())}


@app.get("/api/plugins")
def list_plugins() -> dict[str, object]:
    """插件清单 —— 「功能怎么没出现」第一个该看的地方。"""
    registry = _registry()
    return {
        "summary": registry.summary(),
        "plugins": [
            {
                "id": p.manifest.id,
                "name": p.manifest.name,
                "version": p.manifest.version,
                "capabilities": [str(c) for c in p.manifest.capabilities],
                "handles": [str(h) for h in p.manifest.handles],
                "description": p.manifest.description,
                "usable": p.usable,
                "error": p.error,
            }
            for p in registry
        ],
    }


class SideSpec(BaseModel):
    """一侧的输入。A2L 必填，值集与镜像二选一（或都不给，只比描述信息）。"""

    a2l: str
    values: str | None = None
    image: str | None = None


class CompareRequest(BaseModel):
    baseline: SideSpec
    candidate: SideSpec
    byte_order: str = "big"
    tolerance: float = 0.0
    out: str | None = None


def _side_sources(side: SideSpec, byte_order: str) -> CalibrationSources:
    return CalibrationSources(
        a2l=Path(side.a2l),
        values=Path(side.values) if side.values else None,
        image=Path(side.image) if side.image else None,
        label=Path(side.a2l).name,
        byte_order=byte_order,
    )


def _change_json(change) -> dict[str, object]:
    return {
        "path": change.path,
        "name": change.name,
        "kinds": [str(k) for k in change.kinds],
        "group": change.group,
        "address_before": change.address_before,
        "address_after": change.address_after,
        "values": [
            {
                "index": v.index,
                "before": v.phys_before if v.phys_before is not None else v.raw_before,
                "after": v.phys_after if v.phys_after is not None else v.raw_after,
            }
            for v in change.value_changes[:8]
        ],
        "attributes": [f.display for f in change.attribute_changes],
    }


@app.post("/api/compare")
def compare(req: CompareRequest) -> dict[str, object]:
    """标定比对。逻辑全在 `services.compare`，这里只做参数与序列化。"""
    try:
        outcome = run_compare(
            _registry(),
            _side_sources(req.baseline, req.byte_order),
            _side_sources(req.candidate, req.byte_order),
            out=Path(req.out) if req.out else None,
            params={"tolerance": req.tolerance},
        )
    except Exception as exc:  # noqa: BLE001 - 输入问题要变成人能读的 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    changeset = outcome.changeset
    return {
        "baseline_label": changeset.baseline_label,
        "candidate_label": changeset.candidate_label,
        "summary": {
            "added": len(changeset.added),
            "removed": len(changeset.removed),
            "changed": len(changeset.changed),
            "meaningful": len(changeset.meaningful_changes()),
            "address_moves": len(changeset.address_moves()),
        },
        # 默认只给「需要人看」的改动 —— 地址漂移单独折叠，
        # 否则 App 重编一次，真正的修改会被淹没
        "changes": [_change_json(c) for c in changeset.meaningful_changes()],
        "warnings": list(changeset.warnings),
        "report_path": str(outcome.report_path) if outcome.report_path else None,
    }


class TrendRequest(BaseModel):
    file: str
    signal: str
    points: int = 2000
    start: float | None = None
    end: float | None = None


class WritebackRequest(BaseModel):
    image: str
    a2l: str
    values: str
    out: str | None = None
    byte_order: str = "big"


@app.get("/api/measurement/signals")
def measurement_signals(file: str) -> dict[str, object]:
    """列出测量文件里的信号，供界面下拉选择。"""
    try:
        loaded = load_measurement(_registry(), Path(file))
    except Exception as exc:  # noqa: BLE001 - 输入问题变成人能读的 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    document = loaded.document
    return {
        "label": document.label,
        "warnings": loaded.warnings,
        "signals": [
            {
                "name": s.name,
                "path": s.path,
                "unit": s.unit,
                "kind": str(s.kind),
                "group": s.group,
            }
            for s in document.signals()
        ],
        "events": [
            format_event_key(channel, frame_id)
            for channel, frame_id in sorted(document.event_stats)
        ],
    }


@app.post("/api/trend")
def trend(req: TrendRequest) -> dict[str, object]:
    """抽稀后的趋势。`bands` 是 min/max 带 —— 尖峰不会因抽样丢失。"""
    try:
        loaded = load_measurement(_registry(), Path(req.file))
        window = (
            None
            if req.start is None and req.end is None
            else (req.start or 0.0, req.end or 0.0)
        )
        result = extract_trend(loaded, req.signal, max_points=req.points, window=window)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "signal": result.signal,
        "unit": result.unit,
        "kind": str(result.kind),
        "sample_count": result.sample_count,
        "window": list(result.window) if result.window else None,
        "note": result.note,
        "time_base": result.time_base,
        "stats": result.stats,
        "bands": (
            [
                {
                    "t": [b.t_start, b.t_end],
                    "min": b.v_min,
                    "max": b.v_max,
                    "count": b.count,
                }
                for b in result.bands
            ]
            if result.bands
            else None
        ),
        "series": (
            {
                "times": list(result.times)[:4000],
                "values": list(result.values)[:4000],
            }
            if result.times is not None
            else None
        ),
    }


@app.post("/api/writeback")
def writeback(req: WritebackRequest) -> dict[str, object]:
    """写回。逻辑全在 `services.writeback`，这里只做参数与序列化。"""
    try:
        outcome = run_writeback(
            _registry(),
            WritebackSources(
                image=Path(req.image),
                a2l=Path(req.a2l),
                values=Path(req.values),
                byte_order=req.byte_order,
            ),
            out=Path(req.out) if req.out else None,
        )
    except WritebackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "written": outcome.written,
        "skipped": outcome.skipped,
        "checksums": outcome.checksums,
        "output": str(outcome.output) if outcome.output else None,
        "warnings": outcome.warnings,
    }


def _safe_name(text: str) -> str:
    """把目标标识变成能当文件名用的串。"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def _flash_state_dir() -> Path:
    """刷写状态目录。与 CLI 同一个优先级：环境变量优先，否则应用数据目录。"""
    override = os.environ.get("ADS_FLASH_STATE_DIR")
    return Path(override) if override else default_data_dir()


def _load_space(registry: PluginRegistry, path: Path) -> AddressSpace:
    parsers = registry.parsers_for(kind_from_suffix(path))
    if not parsers:
        raise HTTPException(
            status_code=400, detail=f"没有能解析该格式的插件：{path.suffix}"
        )
    outcome = parsers[0].instance.parse(FileSource(path), JobContext(job_id="api-load"))
    if not isinstance(outcome.document, AddressSpace):
        raise HTTPException(
            status_code=400, detail=f"{path.name} 没有解析出内存镜像"
        )
    return outcome.document


def _need(registry: PluginRegistry, plugin_id: str, what: str):
    plugin = registry.get(plugin_id)
    if plugin is None or not plugin.usable:
        raise HTTPException(status_code=400, detail=f"{what}插件不可用：{plugin_id}")
    return plugin.instance


class FlashRunRequest(BaseModel):
    image: str
    target_id: str = "ECM-01"
    target_name: str = "发动机控制器"
    channel: str = "sim://default"
    operator: str = "web"
    recipe_id: str = "flash-recipe-sim"
    gateway_id: str = "gateway-sim"
    block_size: int = 32


@app.post("/api/flash/run")
def flash_run(req: FlashRunRequest) -> dict[str, object]:
    """发起一次刷写。

    走的是 `services.flash.resume_flash` —— 与 CLI 同一条路：
    断点续刷（不重复擦除）+ 审计留痕（成功与失败都记）。
    """
    registry = _registry()
    recipe = _need(registry, req.recipe_id, "配方")
    gateway = _need(registry, req.gateway_id, "网关")
    space = _load_space(registry, Path(req.image))
    segments = list(space.segments)

    state_dir = _flash_state_dir()
    store = CheckpointStore(
        state_dir / f"checkpoint-{_safe_name(req.target_id)}.json"
    )
    audit = AuditLog(state_dir / "audit.jsonl")

    steps: list[dict[str, object]] = []
    bus = EventBus()
    bus.subscribe(
        Topics.FLASH_STEP,
        lambda e: steps.append(
            {"index": int(e.payload["index"]), "label": e.payload["label"]}
        ),
    )
    ctx = JobContext(job_id=f"flash-api-{_safe_name(req.target_id)}", bus=bus)

    target = FlashTarget(id=req.target_id, name=req.target_name, channel=req.channel)
    fingerprint = sha256_hex(b"".join(s.data for s in segments))

    result = resume_flash(
        recipe=recipe,
        gateway=gateway,
        target=target,
        segments=segments,
        image_fingerprint=fingerprint,
        ctx=ctx,
        checkpoint_store=store,
        audit_log=audit,
        operator=req.operator,
        block_size=req.block_size,
    )
    payload = result.payload or {}
    return {
        "ok": result.state is JobState.SUCCEEDED,
        "state": str(result.state),
        "error": result.error,
        "steps": steps,
        "segments": len(segments),
        "bytes_done": payload.get("bytes_done", 0),
        "resumed_from": payload.get("resumed_from"),
    }


@app.get("/api/flash/history")
def flash_history(target: str | None = None, limit: int = 50) -> dict[str, object]:
    """刷写历史。失败也记 —— 那正是「这台 ECU 是否被反复刷失败过」的答案。"""
    records = AuditLog(_flash_state_dir() / "audit.jsonl").all()
    if target:
        records = [r for r in records if r.target_id == target]
    return {
        "records": [
            {
                "time": r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "target": r.target_id,
                "target_name": r.target_name,
                "operator": r.operator,
                "fingerprint": r.image_fingerprint,
                "success": r.success,
                "error": r.error,
                "segments": f"{r.segments_done}/{r.segments_total}",
                "bytes": f"{r.bytes_done}/{r.bytes_total}",
                "duration_s": round(r.duration_s, 3),
            }
            for r in records[-limit:]
        ]
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """单页应用。没有构建步骤：改完刷新就能看。"""
    return FileResponse(STATIC_DIR / "index.html")


def main() -> None:
    """启动服务。默认只绑回环地址。"""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":  # pragma: no cover - 入口
    main()
