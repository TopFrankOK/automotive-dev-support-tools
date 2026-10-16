"""刷写编排的测试。

整套流程在**仿真网关**上跑通 —— 没有 CAN 卡、没有 ECU。
断言的不是「没抛异常」，而是：

  - 每一步**实际发出的字节**逐条对得上（顺序错一步就是另一件事）
  - 进度数值可核对
  - 不可取消的步骤真的没被中途打断
  - 失败时说清了第几步、为什么

最后一条最要紧：刷写不可重放，含糊的错误会让使用者做出「再试一次」的错误决定。
"""

from __future__ import annotations

import pytest

from automotive_dev_support.core.models.common import sha256_hex
from automotive_dev_support.core.models.flash import FlashTarget
from automotive_dev_support.core.models.memory import Segment
from automotive_dev_support.core.skeleton.event import EventBus, Topics
from automotive_dev_support.core.skeleton.job import (
    CancelledError,
    JobContext,
    JobState,
)
from automotive_dev_support.plugins.flash_recipe_sim import SimRecipe
from automotive_dev_support.plugins.gateway_sim import SimGateway
from automotive_dev_support.services.flash import run_flash

START = 0x8000_0000
IMAGE = b"\xaa" * 64
FINGERPRINT = sha256_hex(IMAGE)


def _segments() -> list[Segment]:
    return [Segment(start=START, data=IMAGE)]


def _target() -> FlashTarget:
    return FlashTarget(id="ECM-01", name="发动机控制器", channel="sim://ecm-1")


def _ok_script() -> list[bytes]:
    """与示例配方一一对应的正常响应序列。"""
    return [
        b"\x50\x02",  # 进入编程会话
        b"\x67\x01\x11\x22",  # 取种子
        b"\x67\x02",  # 密钥通过
        b"\x71\x01\xff\x00",  # 擦除
        b"\x74\x20",  # 请求下载
        b"\x76\x01",  # 第 1 块
        b"\x76\x02",  # 第 2 块
        b"\x71\x01\x02\x02",  # 校验
        b"\x51\x01",  # 复位
    ]


class _Cancel:
    """第 n 次被询问时才喊停。"""

    def __init__(self, nth: int) -> None:
        self.nth = nth
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls >= self.nth


def _run(script, *, cancel=None, block_size=32, **kw):
    bus = EventBus()
    events: list[tuple[str, dict]] = []
    bus.subscribe("flash.", lambda e: events.append((e.topic, dict(e.payload))))
    bus.subscribe(Topics.JOB_PROGRESS, lambda e: events.append((e.topic, dict(e.payload))))

    gateway = SimGateway(script=script)
    ctx = JobContext(job_id="j1", bus=bus, cancel_check=cancel)
    result = run_flash(
        recipe=SimRecipe(),
        gateway=gateway,
        target=_target(),
        segments=_segments(),
        image_fingerprint=FINGERPRINT,
        ctx=ctx,
        operator="zhaolin",
        block_size=block_size,
        **kw,
    )
    return result, gateway, events


class TestHappyPath:
    def test_succeeds(self):
        result, _, _ = _run(_ok_script())
        assert result.state is JobState.SUCCEEDED, result.error

    def test_requests_are_exactly_the_expected_byte_stream(self):
        """顺序错一步就是另一件事，所以逐条核对而不是只看数量。"""
        _, gateway, _ = _run(_ok_script())
        expected = [
            b"\x10\x02",
            b"\x27\x01",
            b"\x27\x02\x4b\x78",  # seed 0x1122 经配方算法得到的密钥
            b"\x31\x01\xff\x00" + START.to_bytes(4, "big") + (64).to_bytes(4, "big"),
            b"\x34\x00\x44" + START.to_bytes(4, "big") + (64).to_bytes(4, "big"),
            b"\x36\x01" + IMAGE[:32],
            b"\x36\x02" + IMAGE[32:],
            b"\x31\x01\x02\x02" + FINGERPRINT.encode("ascii"),
            b"\x11\x01",
        ]
        assert tuple(gateway.requests) == tuple(expected)

    def test_progress_reaches_full(self):
        result, _, _ = _run(_ok_script())
        progress = result.payload["progress"]
        assert progress["bytes_done"] == 64
        assert progress["bytes_total"] == 64
        assert progress["segments_done"] == 1

    def test_step_events_cover_every_step(self):
        _, _, events = _run(_ok_script())
        kinds = [p["kind"] for t, p in events if t == Topics.FLASH_STEP]
        assert kinds == [
            "enter_session",
            "security_access",
            "erase",
            "transfer",
            "verify",
            "reset",
        ]

    def test_transfer_reports_progress_per_block(self):
        _, _, events = _run(_ok_script())
        reported = [p["done"] for t, p in events if t == Topics.JOB_PROGRESS]
        assert 32 in reported
        assert 64 in reported


class TestFailure:
    def test_failure_is_reported_with_step_and_reason(self):
        script = _ok_script()
        script[3] = b"\x7f\x31\x72"  # 擦除：一般编程失败
        result, _, _ = _run(script)
        assert result.state is JobState.FAILED
        assert "擦除" in result.error
        assert "一般编程失败" in result.error

    def test_failure_stops_before_later_steps(self):
        script = _ok_script()
        script[4] = b"\x7f\x34\x31"  # 请求下载：超出范围
        result, gateway, _ = _run(script)
        assert result.state is JobState.FAILED
        # 传输失败之后不该再出现校验与复位的请求
        assert not any(r.startswith(b"\x31\x01\x02") for r in gateway.requests)
        assert b"\x11\x01" not in gateway.requests

    def test_failure_payload_names_the_step(self):
        script = _ok_script()
        script[0] = b"\x7f\x10\x22"
        result, _, _ = _run(script)
        assert result.payload["step_index"] == 0
        assert result.payload["step_kind"] == "enter_session"


class TestCancellation:
    def test_cancellable_step_stops_immediately(self):
        """第一步就喊停：一个字节都不该发出去。"""
        result, gateway, _ = _run(_ok_script(), cancel=_Cancel(1))
        assert result.state is JobState.CANCELLED
        assert tuple(gateway.requests) == ()

    def test_non_cancellable_step_is_not_interrupted(self):
        """擦除中途喊停不许生效 —— 半擦除状态比多等一分钟糟得多。

        取消请求被推迟到该步骤结束，所以：擦除确实执行了，
        而后续传输一步都没走。
        """
        # 第 3 次询问发生在「擦除」执行完之后
        result, gateway, _ = _run(_ok_script(), cancel=_Cancel(3))
        assert result.state is JobState.CANCELLED
        assert any(r.startswith(b"\x31\x01\xff") for r in gateway.requests), (
            "擦除必须跑完"
        )
        assert not any(r.startswith(b"\x34") for r in gateway.requests), (
            "取消后不应继续传输"
        )

    def test_abort_event_is_emitted(self):
        _, _, events = _run(_ok_script(), cancel=_Cancel(3))
        assert any(t == Topics.FLASH_ABORTED for t, _ in events)


class TestRetry:
    def test_retryable_failure_is_retried_within_budget(self):
        """进入编程会话带 1 次重试：第一次「正忙」，第二次成功。"""
        script = [b"\x7f\x10\x78", b"\x50\x02"] + _ok_script()[1:]
        result, gateway, _ = _run(script)
        assert result.state is JobState.SUCCEEDED, result.error
        assert gateway.requests[0] == b"\x10\x02"
        assert gateway.requests[1] == b"\x10\x02"  # 重试了一次

    def test_non_retryable_failure_is_not_retried(self):
        script = [b"\x7f\x10\x33", b"\x50\x02"] + _ok_script()[1:]
        result, gateway, _ = _run(script)
        assert result.state is JobState.FAILED
        assert gateway.requests.count(b"\x10\x02") == 1


class TestBlockSplitting:
    def test_block_size_controls_chunking(self):
        _, gateway, _ = _run(_ok_script()[:5] + [b"\x76\x01"] * 4 + _ok_script()[8:], block_size=16)
        chunks = [r for r in gateway.requests if r.startswith(b"\x36")]
        # 每帧 = 1 字节服务号 + 1 字节序号 + 16 字节数据
        assert [len(c) for c in chunks] == [18, 18, 18, 18]
        assert [c[1] for c in chunks] == [1, 2, 3, 4]


def test_cancelled_error_propagates_as_cancelled_state():
    """取消信号来自外部（界面 / 调度器），编排层只负责转成状态。"""

    def always() -> bool:
        return True

    result, _, _ = _run(_ok_script(), cancel=always)
    assert result.state is JobState.CANCELLED
    assert isinstance(CancelledError(), Exception)
