"""刷写域模型的测试。

刷写是**不可重放**的：标定比对错了可以重跑，刷写错了是物理事故。
所以这一组测试的重点不是「能不能构造出来」，
而是把几条会**安静地错**的语义钉死：

  - 配方指纹必须包含步骤顺序（改顺序不换名字 = 换了配方，却查不出来）
  - 检查点只装游标与指纹（装了镜像数据会让状态文件膨胀到几十 MB）
  - 进度比例必须被夹在 0~1（进度条倒退比不显示更糟）
  - 续刷校验必须指明**哪一项**不一致（只回一句「不能续」等于没说）

断言一律对值，不对「没抛异常」。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from automotive_dev_support.core.models.common import sha256_hex
from automotive_dev_support.core.models.flash import (
    FlashCheckpoint,
    FlashProgress,
    FlashRecipe,
    FlashRecord,
    FlashRegion,
    FlashStep,
    FlashStepKind,
    FlashTarget,
)


def _step(kind: FlashStepKind, **kw) -> FlashStep:
    return FlashStep(kind=kind, **kw)


def _recipe(*kinds: FlashStepKind, **kw) -> FlashRecipe:
    return FlashRecipe(id="r1", steps=tuple(_step(k) for k in kinds), **kw)


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------


class TestFlashStep:
    def test_step_is_immutable(self):
        """步骤一旦建好就不能改 —— 改一步等于换配方，必须走指纹。"""
        step = _step(FlashStepKind.ERASE, timeout_s=30.0)
        with pytest.raises((ValidationError, TypeError, AttributeError)):
            step.timeout_s = 99.0

    def test_defaults_are_conservative(self):
        """默认不可重试、可取消、超时偏短。

        保守的默认值意味着：出问题时先停下拉报错，而不是默默重试到超时。
        """
        step = _step(FlashStepKind.TRANSFER)
        assert step.max_retries == 0
        assert step.cancellable is True
        assert step.timeout_s == 5.0
        assert step.params == {}

    def test_step_kinds_are_the_six_canonical_ones(self):
        assert {k.value for k in FlashStepKind} == {
            "enter_session",
            "security_access",
            "erase",
            "transfer",
            "verify",
            "reset",
        }


# ---------------------------------------------------------------------------
# 配方指纹
# ---------------------------------------------------------------------------


class TestRecipeFingerprint:
    def test_same_recipe_same_fingerprint(self):
        a = _recipe(FlashStepKind.ERASE, FlashStepKind.TRANSFER)
        b = _recipe(FlashStepKind.ERASE, FlashStepKind.TRANSFER)
        assert a.fingerprint == b.fingerprint

    def test_reordering_steps_changes_fingerprint(self):
        """只改顺序、不改内容，也必须算换了配方。

        这是最容易漏的一条：配方名没变、步骤还是那几步，
        但「先擦后传」改成「先传后擦」是致命的，而名字完全看不出来。
        """
        a = _recipe(FlashStepKind.ERASE, FlashStepKind.TRANSFER)
        b = _recipe(FlashStepKind.TRANSFER, FlashStepKind.ERASE)
        assert a.fingerprint != b.fingerprint

    def test_changing_step_params_changes_fingerprint(self):
        a = _recipe(FlashStepKind.TRANSFER)
        b = FlashRecipe(
            id="r1",
            steps=(_step(FlashStepKind.TRANSFER, params={"block": 256}),),
        )
        assert a.fingerprint != b.fingerprint

    def test_changing_timeout_changes_fingerprint(self):
        a = _recipe(FlashStepKind.ERASE)
        b = FlashRecipe(
            id="r1", steps=(_step(FlashStepKind.ERASE, timeout_s=99.0),)
        )
        assert a.fingerprint != b.fingerprint

    def test_different_recipe_id_changes_fingerprint(self):
        a = _recipe(FlashStepKind.ERASE)
        b = FlashRecipe(id="r2", steps=(_step(FlashStepKind.ERASE),))
        assert a.fingerprint != b.fingerprint

    def test_fingerprint_is_short_and_stable_length(self):
        fp = _recipe(FlashStepKind.ERASE, FlashStepKind.VERIFY).fingerprint
        assert fp
        assert len(fp) == 16


# ---------------------------------------------------------------------------
# 目标与区域
# ---------------------------------------------------------------------------


class TestFlashTarget:
    def test_region_end_is_exclusive(self):
        r = FlashRegion(start=0x8000, size=0x100)
        assert r.end == 0x8100

    def test_target_fingerprint_covers_identity_and_layout(self):
        a = FlashTarget(
            id="ECM-01",
            diagnostic_id="0x0E80",
            regions=[FlashRegion(start=0x8000, size=0x100)],
        )
        b = FlashTarget(id="ECM-01", diagnostic_id="0x0E80", regions=[])
        assert a.fingerprint != b.fingerprint

    def test_same_target_same_fingerprint(self):
        kwargs = {
            "id": "ECM-01",
            "diagnostic_id": "0x0E80",
            "regions": [FlashRegion(start=0x8000, size=0x100)],
        }
        assert FlashTarget(**kwargs).fingerprint == FlashTarget(**kwargs).fingerprint


# ---------------------------------------------------------------------------
# 进度
# ---------------------------------------------------------------------------


class TestFlashProgress:
    def test_ratio_from_bytes_when_bytes_known(self):
        p = FlashProgress(bytes_done=250, bytes_total=1000)
        assert p.ratio == 0.25

    def test_ratio_falls_back_to_segments(self):
        p = FlashProgress(segments_done=1, segments_total=4)
        assert p.ratio == 0.25

    def test_ratio_is_none_when_nothing_known(self):
        assert FlashProgress().ratio is None

    def test_ratio_is_clamped(self):
        """进度条倒退比不显示更糟，所以越界必须被夹住。"""
        assert FlashProgress(bytes_done=2000, bytes_total=1000).ratio == 1.0

    def test_zero_total_is_not_a_ratio(self):
        assert FlashProgress(bytes_done=0, bytes_total=0).ratio is None


# ---------------------------------------------------------------------------
# 检查点
# ---------------------------------------------------------------------------


class TestFlashCheckpoint:
    def _cp(self, **kw) -> FlashCheckpoint:
        base = {
            "recipe_fingerprint": "r" * 16,
            "image_fingerprint": "i" * 16,
            "target_fingerprint": "t" * 16,
        }
        base.update(kw)
        return FlashCheckpoint(**base)

    def test_matching_fingerprints_report_no_mismatch(self):
        cp = self._cp()
        assert cp.mismatches("r" * 16, "i" * 16, "t" * 16) == []

    def test_each_fingerprint_is_reported_by_name(self):
        """续刷被拒时必须说清**哪一项**变了。

        只回一句「不能续刷」，使用者就得自己去猜是换了文件还是换了配方。
        """
        cp = self._cp()
        assert cp.mismatches("x" * 16, "i" * 16, "t" * 16) == ["recipe"]
        assert cp.mismatches("r" * 16, "x" * 16, "t" * 16) == ["image"]
        assert cp.mismatches("r" * 16, "i" * 16, "x" * 16) == ["target"]

    def test_all_three_mismatches_are_reported_together(self):
        cp = self._cp()
        assert set(cp.mismatches("a" * 16, "b" * 16, "c" * 16)) == {
            "recipe",
            "image",
            "target",
        }

    def test_checkpoint_holds_no_image_data(self):
        """检查点只装游标与指纹。

        如果把镜像字节塞进来，状态文件会跟着镜像一起膨胀到几十 MB，
        而且下次续刷时读到的到底是哪一个副本就说不清了。
        """
        cp = self._cp(segment_index=3, bytes_done=4096, erased=True)
        dumped = cp.model_dump()
        assert "data" not in dumped
        assert "image" not in dumped
        assert "bytes" not in dumped
        for key, value in dumped.items():
            assert not isinstance(value, (bytes, bytearray)), key

    def test_cursor_and_erased_flag_are_carried(self):
        cp = self._cp(segment_index=5, erased=True)
        assert cp.segment_index == 5
        assert cp.erased is True


# ---------------------------------------------------------------------------
# 审计记录
# ---------------------------------------------------------------------------


class TestFlashRecord:
    def test_finish_sets_duration_and_success(self):
        started = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 10, 15, 10, 5, tzinfo=timezone.utc)
        rec = FlashRecord(job_id="j1", started_at=started)
        rec.finish(success=True, at=ended)
        assert rec.success is True
        assert rec.finished_at == ended
        assert rec.duration_s == 300.0

    def test_failure_keeps_the_step_and_reason(self):
        """失败的刷写同样要留痕，而且要能看出**倒在第几步**。"""
        rec = FlashRecord(job_id="j1", started_at=datetime.now(timezone.utc))
        rec.finish(
            success=False,
            at=datetime.now(timezone.utc),
            error="否定响应 0x7F",
            failed_step_index=2,
            failed_step_kind=FlashStepKind.TRANSFER,
        )
        assert rec.success is False
        assert rec.error == "否定响应 0x7F"
        assert rec.failed_step_index == 2
        assert rec.failed_step_kind is FlashStepKind.TRANSFER

    def test_unfinished_record_is_not_success(self):
        rec = FlashRecord(job_id="j1", started_at=datetime.now(timezone.utc))
        assert rec.success is False
        assert rec.finished_at is None


# ---------------------------------------------------------------------------
# 镜像指纹（内容哈希）
# ---------------------------------------------------------------------------


class TestImageFingerprint:
    def test_same_content_same_fingerprint(self):
        assert sha256_hex(b"abcdef") == sha256_hex(b"abcdef")

    def test_one_byte_differs_fingerprint_differs(self):
        assert sha256_hex(b"abcdef") != sha256_hex(b"abcdeg")
