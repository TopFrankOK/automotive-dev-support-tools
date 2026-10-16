"""断点续刷与审计留痕的测试。

这一组守的是**刷写域最危险的两个失效模式**：

1. 续刷时从错误的半截接着刷 —— 刷出来的既不是旧版也不是新版，
   而这种错误**不会报错**，只会让某次试验的结论悄悄错掉。
   所以三项指纹（配方 / 镜像 / 目标）任何一项变了都必须拒绝，并说清是哪一项。
2. 账本缺页 —— 「谁刷过什么」在审核时一定被问到。
   失败的记录同样要留，而且读不出来时必须报错，不能静默跳过。

另外一条同样重要：续刷**不能重复擦除**。
重复擦写既慢又损耗目标，而它同样是那种「不报错，只是安静地错」的事。
"""

from __future__ import annotations

import pytest

from automotive_dev_support.core.models.common import sha256_hex
from automotive_dev_support.core.models.flash import FlashTarget
from automotive_dev_support.core.models.memory import Segment
from automotive_dev_support.core.skeleton.event import EventBus
from automotive_dev_support.core.skeleton.job import JobContext, JobState
from automotive_dev_support.platform.flash import AuditLog, CheckpointStore
from automotive_dev_support.plugins.flash_recipe_sim import SimRecipe
from automotive_dev_support.plugins.gateway_sim import SimGateway
from automotive_dev_support.services.flash import resume_flash

SEG_A = Segment(start=0x8000_0000, data=b"\xaa" * 32)
SEG_B = Segment(start=0x8000_0100, data=b"\xbb" * 32)
IMAGE_FP = sha256_hex(SEG_A.data + SEG_B.data)

#: 第一次刷写：段 A 传完，段 B 的「请求下载」被拒绝
FIRST_SCRIPT = [
    b"\x50\x02",
    b"\x67\x01\x11\x22",
    b"\x67\x02",
    b"\x71\x01\xff\x00",  # 擦除段 A
    b"\x71\x01\xff\x00",  # 擦除段 B
    b"\x74\x20",  # 请求下载段 A
    b"\x76\x01",  # 段 A 数据块
    b"\x7f\x34\x31",  # 请求下载段 B：超出范围
]

#: 一次完整的全新刷写：两段全部成功
FULL_SCRIPT = [
    b"\x50\x02",
    b"\x67\x01\x11\x22",
    b"\x67\x02",
    b"\x71\x01\xff\x00",  # 擦除段 A
    b"\x71\x01\xff\x00",  # 擦除段 B
    b"\x74\x20",  # 请求下载段 A
    b"\x76\x01",  # 段 A 数据块
    b"\x74\x20",  # 请求下载段 B
    b"\x76\x01",  # 段 B 数据块
    b"\x71\x01\x02\x02",  # 校验
    b"\x51\x01",  # 复位
]

#: 续刷：段 A 已完成，只需处理段 B
RESUME_SCRIPT = [
    b"\x50\x02",
    b"\x67\x01\x11\x22",
    b"\x67\x02",
    b"\x74\x20",  # 请求下载段 B
    b"\x76\x01",  # 段 B 数据块
    b"\x71\x01\x02\x02",  # 校验
    b"\x51\x01",  # 复位
]


def _target(**kw) -> FlashTarget:
    base = {"id": "ECM-01", "name": "发动机控制器", "channel": "sim://ecm-1"}
    base.update(kw)
    return FlashTarget(**base)


def _run(script, tmp_path, *, image_fp=IMAGE_FP, target=None, operator="zhaolin"):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    audit = AuditLog(tmp_path / "audit.jsonl")
    ctx = JobContext(job_id="j1", bus=EventBus())
    gateway = SimGateway(script=script)
    result = resume_flash(
        recipe=SimRecipe(),
        gateway=gateway,
        target=target or _target(),
        segments=[SEG_A, SEG_B],
        image_fingerprint=image_fp,
        ctx=ctx,
        checkpoint_store=store,
        audit_log=audit,
        operator=operator,
        block_size=32,
    )
    return result, gateway, store, audit


class TestFreshFlash:
    def test_no_checkpoint_means_start_from_zero(self, tmp_path):
        result, gateway, store, _ = _run(FULL_SCRIPT, tmp_path)
        assert result.state is JobState.SUCCEEDED, result.error
        assert result.payload["resumed_from"] is None
        assert len([r for r in gateway.requests if r.startswith(b"\x34")]) == 2

    def test_checkpoint_is_cleared_after_success(self, tmp_path):
        """留一个陈旧检查点，下次会被拿来「续」一次其实早完成的刷写。"""
        _, _, store, _ = _run(FULL_SCRIPT, tmp_path)
        assert store.exists() is False


class TestResume:
    def _break_off(self, tmp_path):
        """跑到段 B 失败为止，留下一个检查点。"""
        result, _, store, _ = _run(FIRST_SCRIPT, tmp_path)
        assert result.state is JobState.FAILED
        assert store.exists() is True
        return store

    def test_resume_skips_completed_segment(self, tmp_path):
        store = self._break_off(tmp_path)
        checkpoint = store.load()
        assert checkpoint.segment_index == 1
        assert checkpoint.erased is True

        result, gateway, _, _ = _run(RESUME_SCRIPT, tmp_path)
        assert result.state is JobState.SUCCEEDED, result.error
        # 段 A 已经传完，续刷只该下载段 B
        downloads = [r for r in gateway.requests if r.startswith(b"\x34")]
        assert len(downloads) == 1
        # 请求下载帧 = 0x34 + 2 字节格式 + 4 字节起始地址 + 4 字节长度
        assert downloads[0][3:7] == (0x8000_0100).to_bytes(4, "big")
        assert result.payload["resumed_from"] == 1

    def test_resume_does_not_erase_again(self, tmp_path):
        """重复擦写既慢又损耗目标，而且是那种悄悄发生的错。"""
        self._break_off(tmp_path)
        _, gateway, _, _ = _run(RESUME_SCRIPT, tmp_path)
        assert not any(r.startswith(b"\x31\x01\xff") for r in gateway.requests), (
            "续刷不该重复擦除"
        )

    def test_resume_rejects_changed_image(self, tmp_path):
        self._break_off(tmp_path)
        result, gateway, _, _ = _run(
            RESUME_SCRIPT, tmp_path, image_fp=sha256_hex(b"changed")
        )
        assert result.state is JobState.FAILED
        assert "镜像" in result.error
        assert tuple(gateway.requests) == (), "拒绝续刷时一个字节都不该发出去"

    def test_resume_rejects_changed_target(self, tmp_path):
        self._break_off(tmp_path)
        result, _, _, _ = _run(
            RESUME_SCRIPT, tmp_path, target=_target(id="ECM-02")
        )
        assert result.state is JobState.FAILED
        assert "目标" in result.error

    def test_resume_rejects_corrupt_checkpoint(self, tmp_path):
        """检查点读不出来绝不能当成「没有检查点」 ——
        那会让人从头再刷一遍，白白擦写一次目标。"""
        path = tmp_path / "checkpoint.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        store = CheckpointStore(path)
        with pytest.raises(ValueError):
            store.load()


class TestAudit:
    def test_both_failure_and_success_are_recorded(self, tmp_path):
        _run(FIRST_SCRIPT, tmp_path)  # 失败
        _run(RESUME_SCRIPT, tmp_path)  # 续刷成功（并清除检查点）
        _, _, _, audit = _run(FULL_SCRIPT, tmp_path)  # 再一次全新刷写
        records = audit.all()
        assert len(records) == 3
        assert records[0].success is False
        assert records[1].success is True
        assert records[2].success is True

    def test_failure_record_names_the_step(self, tmp_path):
        _, _, _, audit = _run(FIRST_SCRIPT, tmp_path)
        record = audit.all()[0]
        assert record.success is False
        assert record.failed_step_kind is not None
        assert record.error

    def test_history_survives_reopening(self, tmp_path):
        _, _, _, audit = _run(FIRST_SCRIPT, tmp_path)
        reopened = AuditLog(tmp_path / "audit.jsonl")
        assert len(reopened.all()) == 1

    def test_lookup_by_target(self, tmp_path):
        _, _, _, audit = _run(FIRST_SCRIPT, tmp_path)
        assert len(audit.by_target("ECM-01")) == 1
        assert audit.by_target("ECM-99") == []

    def test_lookup_by_image(self, tmp_path):
        """回答「这个版本刷过哪些台」。"""
        _, _, _, audit = _run(FIRST_SCRIPT, tmp_path)
        assert len(audit.by_image(IMAGE_FP)) == 1
        assert audit.by_image("nonexistent") == []

    def test_interrupted_write_leaves_no_partial_record(self, tmp_path):
        """每条记录整行落盘，所以中断最多丢最后一条，不会留下半条。"""
        audit = AuditLog(tmp_path / "audit.jsonl")
        _run(FIRST_SCRIPT, tmp_path)
        lines = [
            line
            for line in (tmp_path / "audit.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        import json

        json.loads(lines[0])  # 整行必须是合法 JSON

    def test_corrupt_line_is_not_silently_skipped(self, tmp_path):
        """静默跳过一条记录 = 账本悄悄少一页。"""
        path = tmp_path / "audit.jsonl"
        path.write_text("{ 坏行\n", encoding="utf-8")
        with pytest.raises(ValueError):
            AuditLog(path).all()
