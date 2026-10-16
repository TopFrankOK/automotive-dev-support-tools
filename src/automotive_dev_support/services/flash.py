"""刷写用例编排 —— 把配方、网关、镜像组装成一次刷写作业。

**这一层不认识任何协议，也不认识任何硬件。**
它只做四件事：按配方推进、汇报进度、保护不可取消的步骤、失败即停并说清原因。

## 依赖注入是刻意的

配方与网关由调用方传入实例，本模块**不 import 任何插件**。
换来的是：整套编排可以在没有 CAN 卡、没有 ECU 的机器上被完整验证 ——
注入一个仿真网关即可。

## 三条不能让步的规则

1. **不可取消的步骤必须跑完。**
   擦除或传输中途停手会把目标留在半擦除 / 半写入状态，那比多等一分钟糟得多。
   所以取消请求在这类步骤上被**推迟到步骤结束**，而不是立即中断。
2. **失败即停，且必须说清第几步、为什么。**
   刷写不可重放，含糊的错误信息会让人做出「再试一次」的错误决定。
3. **只有「等一等就会好」的失败才重试。**
   密钥错误重试一百次还是错的，还会把目标锁死。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from automotive_dev_support.core.models.common import now_utc
from automotive_dev_support.core.models.flash import (
    FlashCheckpoint,
    FlashProgress,
    FlashRecord,
    FlashStep,
    FlashStepKind,
    FlashStepOutcome,
    FlashTarget,
)
from automotive_dev_support.core.models.memory import Segment
from automotive_dev_support.platform.flash import AuditLog, CheckpointStore
from automotive_dev_support.core.skeleton.event import Topics
from automotive_dev_support.core.skeleton.job import (
    CancelledError,
    JobContext,
    JobResult,
    JobState,
)

__all__ = ["DEFAULT_BLOCK_SIZE", "run_flash", "resume_flash"]


DEFAULT_BLOCK_SIZE = 32
"""默认分块大小。真机上会大得多，这里取小值是为了让流程可被逐步验证。"""


class _Recipe(Protocol):
    recipe_id: str

    def describe(self) -> Any: ...

    def encode(self, step: Any, ctx: dict[str, Any]) -> bytes: ...

    def interpret(self, step: Any, response: bytes) -> Any: ...


class _Gateway(Protocol):
    def connect(self, params: dict[str, Any]) -> None: ...

    def disconnect(self) -> None: ...

    def request(self, payload: bytes, timeout_s: float = 1.0) -> bytes: ...


def run_flash(
    *,
    recipe: _Recipe,
    gateway: _Gateway,
    target: FlashTarget,
    segments: Sequence[Segment],
    image_fingerprint: str,
    ctx: JobContext,
    operator: str = "",
    block_size: int = DEFAULT_BLOCK_SIZE,
    start_segment_index: int = 0,
    erased: bool = False,
    checkpoint_hook: Callable[[int, int, bool], None] | None = None,
) -> JobResult:
    """执行一次刷写。

    `start_segment_index` 与 `erased` 供断点续刷使用：
    已完成的段会被跳过，已擦过的区域不会重复擦。

    `checkpoint_hook(segment_index, bytes_done, erased)` 在每个**安全边界**
    （擦除完成、每段传输完成）被调用，由调用方决定要不要落盘。
    """
    segs = list(segments)
    total_bytes = sum(len(s.data) for s in segs)

    progress = FlashProgress(
        recipe_id=getattr(recipe, "recipe_id", ""),
        segments_total=len(segs),
        bytes_total=total_bytes,
    )

    def emit(message: str = "") -> None:
        if message:
            progress.message = message
        ctx.emit(Topics.FLASH_PROGRESS, **progress.model_dump(mode="json"))
        ctx.report(progress.bytes_done, progress.bytes_total, progress.message)

    try:
        gateway.connect(
            {"channel": target.channel, "diagnostic_id": target.diagnostic_id}
        )
    except Exception as exc:  # 连接阶段任何异常都不能让调用方炸掉
        return JobResult.failed(f"连接失败：{exc}")

    try:
        steps = recipe.describe().steps
        for index, step in enumerate(steps):
            # 可取消的步骤：进来之前先看有没有人喊停
            if step.cancellable:
                ctx.raise_if_cancelled()

            progress.step_index = index
            progress.step_kind = step.kind
            progress.message = step.label or str(step.kind)
            ctx.emit(
                Topics.FLASH_STEP,
                index=index,
                kind=str(step.kind),
                label=progress.message,
            )

            outcome = _run_step(
                recipe=recipe,
                gateway=gateway,
                step=step,
                segments=segs,
                image_fingerprint=image_fingerprint,
                progress=progress,
                block_size=block_size,
                start_segment_index=start_segment_index,
                erased=erased,
                emit=emit,
                checkpoint_hook=checkpoint_hook,
            )

            if not outcome.ok:
                return JobResult.failed(
                    f"第 {index + 1} 步（{progress.message}）失败：{outcome.detail}",
                    payload={
                        "step_index": index,
                        "step_kind": str(step.kind),
                        "detail": outcome.detail,
                        "progress": progress.model_dump(mode="json"),
                        "operator": operator,
                        "image_fingerprint": image_fingerprint,
                        "target_id": target.id,
                    },
                )

            # 不可取消的步骤：跑完之后才响应取消请求
            if not step.cancellable:
                ctx.raise_if_cancelled()

        ctx.emit(Topics.FLASH_PROGRESS, **progress.model_dump(mode="json"))
        return JobResult.ok(
            payload={
                "progress": progress.model_dump(mode="json"),
                "operator": operator,
                "image_fingerprint": image_fingerprint,
                "target_id": target.id,
                "segments_done": progress.segments_done,
                "bytes_done": progress.bytes_done,
            }
        )
    except CancelledError:
        ctx.emit(Topics.FLASH_ABORTED, step_index=progress.step_index)
        return JobResult(
            state=JobState.CANCELLED,
            payload={
                "progress": progress.model_dump(mode="json"),
                "image_fingerprint": image_fingerprint,
                "target_id": target.id,
                "segments_done": progress.segments_done,
                "bytes_done": progress.bytes_done,
            },
        )
    finally:
        gateway.disconnect()


# ---------------------------------------------------------------------------
# 单步执行
# ---------------------------------------------------------------------------


def _run_step(
    *,
    recipe: _Recipe,
    gateway: _Gateway,
    step: FlashStep,
    segments: list[Segment],
    image_fingerprint: str,
    progress: FlashProgress,
    block_size: int,
    start_segment_index: int,
    erased: bool,
    emit: Any,
    checkpoint_hook: Callable[[int, int, bool], None] | None = None,
) -> FlashStepOutcome:
    kind = FlashStepKind(step.kind)

    if kind is FlashStepKind.ENTER_SESSION:
        return _once(recipe, gateway, step, {})

    if kind is FlashStepKind.SECURITY_ACCESS:
        return _security_access(recipe, gateway, step)

    if kind is FlashStepKind.ERASE:
        if erased:
            # 续刷时重复擦写既慢又损耗目标，跳过
            return FlashStepOutcome(ok=True, kind=kind, detail="已擦除（跳过）")
        for seg in segments:
            outcome = _once(
                recipe,
                gateway,
                step,
                {"start": seg.start, "size": len(seg.data)},
            )
            if not outcome.ok:
                return outcome
        if checkpoint_hook is not None:
            checkpoint_hook(0, 0, True)
        return FlashStepOutcome(ok=True, kind=kind, detail="擦除完成")

    if kind is FlashStepKind.TRANSFER:
        return _transfer(
            recipe=recipe,
            gateway=gateway,
            step=step,
            segments=segments,
            progress=progress,
            block_size=block_size,
            start_segment_index=start_segment_index,
            emit=emit,
            checkpoint_hook=checkpoint_hook,
        )

    if kind is FlashStepKind.VERIFY:
        return _once(
            recipe, gateway, step, {"image_fingerprint": image_fingerprint}
        )

    if kind is FlashStepKind.RESET:
        return _once(recipe, gateway, step, {})

    return FlashStepOutcome(ok=False, kind=kind, detail=f"未支持的步骤：{kind!r}")


def _once(
    recipe: _Recipe,
    gateway: _Gateway,
    step: FlashStep,
    step_ctx: dict[str, Any],
) -> FlashStepOutcome:
    """执行一步（含重试）。只有「等一等就会好」的失败才重试。"""
    outcome: FlashStepOutcome | None = None
    for _attempt in range(step.max_retries + 1):
        outcome = _request_once(recipe, gateway, step, step_ctx)
        if outcome.ok or not outcome.retryable:
            return outcome
    assert outcome is not None
    return outcome


def _request_once(
    recipe: _Recipe,
    gateway: _Gateway,
    step: FlashStep,
    step_ctx: dict[str, Any],
) -> FlashStepOutcome:
    payload = recipe.encode(step, step_ctx)
    try:
        response = gateway.request(payload, timeout_s=step.timeout_s)
    except TimeoutError:
        return FlashStepOutcome(
            ok=False,
            kind=step.kind,
            detail=f"超时（{step.timeout_s}s）",
            retryable=True,
        )
    except ConnectionError as exc:
        return FlashStepOutcome(
            ok=False, kind=step.kind, detail=f"链路断开：{exc}", retryable=True
        )
    return recipe.interpret(step, response)


def _security_access(
    recipe: _Recipe, gateway: _Gateway, step: FlashStep
) -> FlashStepOutcome:
    """安全访问：先取种子，再用种子换密钥。

    种子**由配方插件从响应里指出**，编排层不知道响应格式 ——
    「哪几个字节是种子」是协议知识。
    """
    first = _once(recipe, gateway, step, {})
    if not first.ok:
        return first
    seed = bytes(first.data or b"")
    if not seed:
        return FlashStepOutcome(
            ok=False,
            kind=step.kind,
            detail="取种子成功但没有拿到种子字节",
            response=first.response,
        )
    return _once(recipe, gateway, step, {"seed": seed})


def _transfer(
    *,
    recipe: _Recipe,
    gateway: _Gateway,
    step: FlashStep,
    segments: list[Segment],
    progress: FlashProgress,
    block_size: int,
    start_segment_index: int,
    emit: Any,
    checkpoint_hook: Callable[[int, int, bool], None] | None = None,
) -> FlashStepOutcome:
    done_bytes = 0
    for seg_index, seg in enumerate(segments):
        if seg_index < start_segment_index:
            # 续刷：这些段上次已经传完了，直接把进度补上
            done_bytes += len(seg.data)
            progress.segments_done = seg_index + 1
            progress.bytes_done = done_bytes
            continue

        request_download = _once(
            recipe, gateway, step, {"start": seg.start, "size": len(seg.data)}
        )
        if not request_download.ok:
            return request_download

        for seq, offset in enumerate(range(0, len(seg.data), block_size), start=1):
            chunk = seg.data[offset : offset + block_size]
            outcome = _once(recipe, gateway, step, {"chunk": chunk, "seq": seq})
            if not outcome.ok:
                return outcome
            done_bytes += len(chunk)
            progress.bytes_done = done_bytes
            emit(f"传输中 {done_bytes}/{progress.bytes_total} 字节")

        progress.segments_done = seg_index + 1
        progress.bytes_done = done_bytes
        if checkpoint_hook is not None:
            checkpoint_hook(seg_index + 1, done_bytes, True)
        emit(f"完成第 {seg_index + 1}/{len(segments)} 段")

    return FlashStepOutcome(ok=True, kind=step.kind, detail="传输完成")


# ---------------------------------------------------------------------------
# 带断点续刷与审计的入口
# ---------------------------------------------------------------------------


def resume_flash(
    *,
    recipe: _Recipe,
    gateway: _Gateway,
    target: FlashTarget,
    segments: Sequence[Segment],
    image_fingerprint: str,
    ctx: JobContext,
    checkpoint_store: CheckpointStore,
    audit_log: AuditLog,
    operator: str = "",
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> JobResult:
    """带断点续刷与审计留痕的刷写入口。

    续刷前逐项校验配方、镜像、目标三项指纹。
    **任何一项不一致就拒绝续刷并说清是哪一项** ——
    从半截接着刷出一个「既不是旧版也不是新版」的目标，
    是这类系统里后果最严重的静默错误。
    """
    recipe_fingerprint = recipe.describe().fingerprint
    target_fingerprint = target.fingerprint
    recipe_id = getattr(recipe, "recipe_id", "")
    started = now_utc()

    checkpoint = checkpoint_store.load()
    if checkpoint is not None:
        bad = checkpoint.mismatches(
            recipe_fingerprint, image_fingerprint, target_fingerprint
        )
        if bad:
            names = {"recipe": "配方", "image": "镜像", "target": "目标"}
            return JobResult.failed(
                "拒绝续刷："
                + "、".join(names[b] for b in bad)
                + "与上次不一致。（检查点仍在，确认无误后可先清除再刷。）"
            )

    start_segment_index = checkpoint.segment_index if checkpoint else 0
    erased = bool(checkpoint.erased) if checkpoint else False

    def hook(segment_index: int, bytes_done: int, is_erased: bool) -> None:
        checkpoint_store.save(
            FlashCheckpoint(
                job_id=ctx.job_id,
                recipe_id=recipe_id,
                recipe_fingerprint=recipe_fingerprint,
                image_fingerprint=image_fingerprint,
                target_fingerprint=target_fingerprint,
                segment_index=segment_index,
                bytes_done=bytes_done,
                erased=is_erased,
            )
        )

    result = run_flash(
        recipe=recipe,
        gateway=gateway,
        target=target,
        segments=segments,
        image_fingerprint=image_fingerprint,
        ctx=ctx,
        operator=operator,
        block_size=block_size,
        start_segment_index=start_segment_index,
        erased=erased,
        checkpoint_hook=hook,
    )

    payload = result.payload or {}
    record = FlashRecord(
        job_id=ctx.job_id,
        started_at=started,
        operator=operator,
        target_id=target.id,
        target_name=target.name,
        image_fingerprint=image_fingerprint,
        recipe_id=recipe_id,
        recipe_fingerprint=recipe_fingerprint,
        segments_done=int(payload.get("segments_done", 0) or 0),
        segments_total=len(list(segments)),
        bytes_done=int(payload.get("bytes_done", 0) or 0),
        bytes_total=sum(len(s.data) for s in segments),
    )

    if result.state is JobState.SUCCEEDED:
        record.finish(success=True, at=now_utc())
        # 清掉检查点：留一个陈旧检查点，下次会被拿来「续」一次早完成的刷写
        checkpoint_store.clear()
    elif result.state is JobState.CANCELLED:
        record.finish(success=False, at=now_utc(), error="已被取消")
    else:
        record.finish(
            success=False,
            at=now_utc(),
            error=result.error or "",
            failed_step_index=payload.get("step_index"),
            failed_step_kind=(
                FlashStepKind(payload["step_kind"])
                if payload.get("step_kind")
                else None
            ),
        )

    audit_log.append(record)
    payload["audit"] = record.model_dump(mode="json")
    payload["resumed_from"] = start_segment_index if checkpoint else None
    return result
