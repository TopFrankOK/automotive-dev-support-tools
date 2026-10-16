"""示例配方插件的测试。

这个插件是**协议专有知识的唯一落脚点**，所以这里测的是两件事：

1. 语义步骤被翻译成**可逐字节核对**的请求
   （断言「发出了东西」是不够的，那等于没测）
2. 各种失败被翻译成**人能读的原因**，且原始字节一个都没丢

第二条里最要紧的是：认不出来的响应必须算**失败**。
把看不懂的响应当成成功，等于让一次实际上失败的刷写通过验收。
"""

from __future__ import annotations

import pytest

from automotive_dev_support.core.models.flash import FlashStep, FlashStepKind
from automotive_dev_support.plugins.flash_recipe_sim import SimRecipe


@pytest.fixture
def recipe() -> SimRecipe:
    return SimRecipe()


class TestDescribe:
    def test_steps_are_in_canonical_order(self, recipe):
        kinds = [s.kind for s in recipe.describe().steps]
        assert kinds == [
            FlashStepKind.ENTER_SESSION,
            FlashStepKind.SECURITY_ACCESS,
            FlashStepKind.ERASE,
            FlashStepKind.TRANSFER,
            FlashStepKind.VERIFY,
            FlashStepKind.RESET,
        ]

    def test_erase_and_transfer_are_not_cancellable(self, recipe):
        """擦除与传输中途停手 = 把目标留在半擦除 / 半写入状态。"""
        for step in recipe.describe().steps:
            if step.kind in (FlashStepKind.ERASE, FlashStepKind.TRANSFER):
                assert step.cancellable is False, step.kind

    def test_other_steps_are_cancellable(self, recipe):
        for step in recipe.describe().steps:
            if step.kind not in (FlashStepKind.ERASE, FlashStepKind.TRANSFER):
                assert step.cancellable is True, step.kind

    def test_fingerprint_is_stable(self, recipe):
        assert recipe.describe().fingerprint == recipe.describe().fingerprint


class TestEncode:
    def test_enter_session(self, recipe):
        step = FlashStep(kind=FlashStepKind.ENTER_SESSION)
        assert recipe.encode(step, {}) == b"\x10\x02"

    def test_security_access_requests_seed_first(self, recipe):
        """还没拿到种子时只能请求种子。"""
        step = FlashStep(kind=FlashStepKind.SECURITY_ACCESS)
        assert recipe.encode(step, {}) == b"\x27\x01"

    def test_security_access_derives_key_from_seed(self, recipe):
        """密钥算法各家私有，属于配方的知识 —— 编排层不参与。"""
        step = FlashStep(kind=FlashStepKind.SECURITY_ACCESS)
        out = recipe.encode(step, {"seed": b"\x11\x22"})
        assert out == b"\x27\x02\x4b\x78"  # 0x11^0x5A, 0x22^0x5A

    def test_erase_carries_address_and_length(self, recipe):
        step = FlashStep(kind=FlashStepKind.ERASE)
        out = recipe.encode(step, {"start": 0x8000_0000, "size": 0x1000})
        assert out[:4] == b"\x31\x01\xff\x00"
        assert out[4:8] == (0x8000_0000).to_bytes(4, "big")
        assert out[8:12] == (0x1000).to_bytes(4, "big")

    def test_transfer_requests_download_before_data(self, recipe):
        step = FlashStep(kind=FlashStepKind.TRANSFER)
        out = recipe.encode(step, {"start": 0x8000_0000, "size": 64})
        assert out[0] == 0x34

    def test_transfer_carries_block_sequence_and_chunk(self, recipe):
        step = FlashStep(kind=FlashStepKind.TRANSFER)
        out = recipe.encode(step, {"chunk": b"\xaa\xbb", "seq": 3})
        assert out == b"\x36\x03\xaa\xbb"

    def test_verify_carries_the_image_fingerprint(self, recipe):
        """校验必须有据可依 —— 指纹进了请求，回读才有得比。"""
        step = FlashStep(kind=FlashStepKind.VERIFY)
        out = recipe.encode(step, {"image_fingerprint": "abcdef0123456789"})
        assert out[:4] == b"\x31\x01\x02\x02"
        assert out[4:] == b"abcdef0123456789"

    def test_reset(self, recipe):
        step = FlashStep(kind=FlashStepKind.RESET)
        assert recipe.encode(step, {}) == b"\x11\x01"


class TestInterpret:
    def test_positive_response_is_success(self, recipe):
        step = FlashStep(kind=FlashStepKind.ENTER_SESSION)
        out = recipe.interpret(step, b"\x50\x02")
        assert out.ok is True

    def test_negative_response_is_translated_to_readable_text(self, recipe):
        step = FlashStep(kind=FlashStepKind.SECURITY_ACCESS)
        out = recipe.interpret(step, b"\x7f\x27\x35")
        assert out.ok is False
        assert "密钥" in out.detail

    def test_missing_seed_and_wrong_key_are_distinct(self, recipe):
        """「还没请求种子」和「密钥错了」是两种完全不同的处理。"""
        step = FlashStep(kind=FlashStepKind.SECURITY_ACCESS)
        denied = recipe.interpret(step, b"\x7f\x27\x33")
        wrong = recipe.interpret(step, b"\x7f\x27\x35")
        assert denied.detail != wrong.detail
        assert "种子" in denied.detail

    def test_only_waitable_failures_are_retryable(self, recipe):
        step = FlashStep(kind=FlashStepKind.TRANSFER)
        assert recipe.interpret(step, b"\x7f\x36\x78").retryable is True  # 正忙
        assert recipe.interpret(step, b"\x7f\x27\x35").retryable is False  # 密钥错

    def test_unknown_negative_code_is_a_failure_and_keeps_raw_bytes(self, recipe):
        """认不出来的响应必须算失败，且原始字节一个都不能丢。"""
        step = FlashStep(kind=FlashStepKind.TRANSFER)
        out = recipe.interpret(step, b"\x7f\x36\x99")
        assert out.ok is False
        assert out.detail
        assert out.response == b"\x7f\x36\x99"

    def test_empty_response_is_a_retryable_failure(self, recipe):
        step = FlashStep(kind=FlashStepKind.TRANSFER)
        out = recipe.interpret(step, b"")
        assert out.ok is False
        assert out.retryable is True

    def test_mismatched_response_keeps_raw_bytes(self, recipe):
        """响应与请求对不上：宁可报错，也不能当成成功。"""
        step = FlashStep(kind=FlashStepKind.RESET)
        out = recipe.interpret(step, b"\x50\x02")
        assert out.ok is False
        assert out.response == b"\x50\x02"


class TestManifest:
    def test_is_discovered_as_a_recipe_plugin(self):
        from automotive_dev_support.core.ports.registry import PluginRegistry
        from conftest import PLUGINS_DIR

        registry = PluginRegistry()
        registry.load_all([PLUGINS_DIR])
        plugin = next(
            (p for p in registry if p.manifest.id == "flash-recipe-sim"), None
        )
        assert plugin is not None
        assert plugin.error is None, f"插件加载失败：{plugin.error}"
        assert "recipe" in [c.value for c in plugin.manifest.capabilities]
