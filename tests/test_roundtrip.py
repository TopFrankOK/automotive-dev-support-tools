"""端到端往返测试：解析 → 写回 → 解析。

**这是本阶段最值钱的一份测试**，因为它直接回答了那个唯一重要的问题：

> 改了一个量之后，其它字节是不是真的一个都没动？

单测能证明「写值正确」，但证明不了「没碰别的地方」。
而后者才是无损往返的全部含义 —— 碰了一个不该碰的字节，
值还是对的，diff 却是红的，评审没法做。

校验方式刻意做得笨一点：**逐字节比对**，跳过被改动的那些区间。
"""

from __future__ import annotations

import shutil

from conftest import SAMPLE_DATA
from automotive_dev_support.core.models.calibration import CalibrationModel
from automotive_dev_support.core.models.calibration.values import bind_image
from automotive_dev_support.core.models.memory import AddressSpace
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.core.skeleton import JobContext
from automotive_dev_support.platform.store import FileSource
from automotive_dev_support.plugins.parser_hex import HexParser
from automotive_dev_support.services.writeback import WritebackSources, run_writeback
from automotive_dev_support.plugins.parser_a2l import A2lParser

CTX = JobContext(job_id="roundtrip")


def _registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load_all([SAMPLE_DATA.parents[1] / "src" / "automotive_dev_support" / "plugins"])
    return reg


def _parse_hex(path) -> AddressSpace:
    outcome = HexParser().parse(FileSource(path), CTX)
    assert not outcome.warnings, outcome.warnings
    return outcome.document


def _parse_a2l(path) -> CalibrationModel:
    return A2lParser().parse(FileSource(path), CTX).document


def _copy(tmp_path, name: str):
    target = tmp_path / name
    shutil.copy(SAMPLE_DATA / name, target)
    return target


def _ranges_of(model: CalibrationModel, paths: list[str]):
    """被改动的地址区间。比对时要跳过它们。"""
    spans = []
    for path in paths:
        char = model.characteristics.get(path)
        if char is None or char.address is None:
            continue
        size = char.element_count * char.data_type.byte_size
        spans.append((char.address, char.address + size))
    return spans


class TestUnchangedRoundTrip:
    def test_writing_back_the_same_values_restores_the_file(self, tmp_path):
        """v1 的值集与镜像一致 → 原值写回应**字节级还原**。

        这是无损往返最硬的一条断言：不增不减、不重排、不改行尾。

        v2 不适用这一条 —— 它的值集与镜像本就差一个字节
        （样例数据本身如此，不是写回的问题），v2 走下面「值写进去读回来要相等」。
        """
        name = "sample_v1.hex"
        image = _copy(tmp_path, name)
        outcome = run_writeback(
            _registry(),
            WritebackSources(
                image=image,
                a2l=SAMPLE_DATA / name.replace(".hex", ".a2l"),
                values=SAMPLE_DATA / name.replace(".hex", ".cdfx"),
            ),
        )
        assert outcome.written, outcome.skipped
        assert image.read_bytes() == (SAMPLE_DATA / name).read_bytes()

    def test_v2_output_is_still_a_valid_image(self, tmp_path):
        """v2 写回后仍要能被解析器接受（行格式、checksum 都不能坏）。"""
        image = _copy(tmp_path, "sample_v2.hex")
        run_writeback(
            _registry(),
            WritebackSources(
                image=image,
                a2l=SAMPLE_DATA / "sample_v2.a2l",
                values=SAMPLE_DATA / "sample_v2.cdfx",
            ),
        )
        parsed = _parse_hex(image)
        assert parsed.coverage()["bytes"] == _parse_hex(
            SAMPLE_DATA / "sample_v2.hex"
        ).coverage()["bytes"]


class TestChangedBytes:
    def test_only_the_rewritten_quantities_differ(self, tmp_path):
        image = _copy(tmp_path, "sample_v1.hex")
        before = _parse_hex(SAMPLE_DATA / "sample_v1.hex")

        outcome = run_writeback(
            _registry(),
            WritebackSources(
                image=image,
                a2l=SAMPLE_DATA / "sample_v1.a2l",
                values=SAMPLE_DATA / "sample_v2.cdfx",
            ),
        )
        assert outcome.written

        after = _parse_hex(image)
        model = _parse_a2l(SAMPLE_DATA / "sample_v1.a2l")
        spans = _ranges_of(model, outcome.written)

        checked = 0
        for segment in before.segments:
            for offset in range(len(segment.data)):
                address = segment.start + offset
                if any(lo <= address < hi for lo, hi in spans):
                    continue
                assert after.read(address, 1) == before.read(address, 1), (
                    f"地址 0x{address:X} 的字节被改动了，但它不属于任何被改写的量"
                )
                checked += 1
        assert checked > 0, "一个字节都没比对，测试本身有问题"

    def test_rewritten_values_can_be_read_back(self, tmp_path):
        image = _copy(tmp_path, "sample_v1.hex")
        outcome = run_writeback(
            _registry(),
            WritebackSources(
                image=image,
                a2l=SAMPLE_DATA / "sample_v1.a2l",
                values=SAMPLE_DATA / "sample_v2.cdfx",
            ),
        )
        after, _ = bind_image(_parse_a2l(SAMPLE_DATA / "sample_v1.a2l"), _parse_hex(image))
        for path in outcome.written:
            assert after.get(path).raw_values is not None, f"{path} 写回后读不到值"
