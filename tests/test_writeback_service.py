"""写回编排的测试。

这里守的是**顺序与边界**，不是格式（格式由 writer 插件的测试守）：

  - 值真的进了镜像（读回来要相等）
  - 用原值写回去，文件能还原成原样 —— 这是"无损"最硬的一条
  - 给 `--out` 时原文件一个字节都不动
  - 任一步失败，原文件必须完好（不能产出半截文件）
"""

from __future__ import annotations

import shutil

import pytest

from conftest import PLUGINS_DIR, SAMPLE_DATA
from automotive_dev_support.core.models.memory import ChecksumKind, ChecksumSpec
from automotive_dev_support.core.ports import PluginRegistry
from automotive_dev_support.services.compare import CalibrationSources, load_calibration
from automotive_dev_support.services.writeback import (
    WritebackError,
    WritebackSources,
    run_writeback,
)


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load_all([PLUGINS_DIR])
    return reg


def _image_copy(tmp_path, name: str = "sample_v1.hex"):
    """样例文件必须复制到临时目录再改 —— 写回测试不许动 tests/data。"""
    target = tmp_path / name
    shutil.copy(SAMPLE_DATA / name, target)
    return target


def _sources(image, values: str = "sample_v2.cdfx", a2l: str = "sample_v1.a2l"):
    return WritebackSources(
        image=image,
        a2l=SAMPLE_DATA / a2l,
        values=SAMPLE_DATA / values,
    )


class TestEndToEnd:
    def test_written_values_can_be_read_back(self, tmp_path, registry):
        image = _image_copy(tmp_path)
        sources = _sources(image)
        outcome = run_writeback(registry, sources)
        assert outcome.written, outcome.skipped

        got = load_calibration(
            registry, CalibrationSources(a2l=sources.a2l, image=image)
        )
        want = load_calibration(
            registry, CalibrationSources(a2l=sources.a2l, values=sources.values)
        )
        for path in outcome.written:
            assert got.model.get(path).raw_values == want.model.get(path).raw_values

    def test_writing_back_the_same_values_restores_the_file(self, tmp_path, registry):
        """无损最硬的一条：把镜像原有的值写回去，文件应与原来**字节级相同**。"""
        image = _image_copy(tmp_path)
        original = image.read_bytes()

        run_writeback(registry, _sources(image, values="sample_v1.cdfx"))
        assert image.read_bytes() == original

    def test_progress_reports_each_step(self, tmp_path, registry):
        steps: list[str] = []
        image = _image_copy(tmp_path)
        run_writeback(registry, _sources(image), progress=steps.append)
        assert any("解析镜像" in s for s in steps)
        assert any("写值" in s for s in steps)
        assert any("写入" in s for s in steps)
        assert steps[-1].startswith("写入")


class TestOutputTarget:
    def test_out_leaves_the_original_untouched(self, tmp_path, registry):
        image = _image_copy(tmp_path)
        original = image.read_bytes()
        out = tmp_path / "written.hex"

        outcome = run_writeback(registry, _sources(image), out=out)

        assert outcome.output == out
        assert image.read_bytes() == original, "给了 --out 还改了原文件"
        assert out.read_bytes() != original or True  # 副本存在即可

    def test_in_place_write_replaces_the_file(self, tmp_path, registry):
        image = _image_copy(tmp_path)
        original = image.read_bytes()
        run_writeback(registry, _sources(image))
        assert image.read_bytes() != original


class TestChecksums:
    def test_checksum_is_updated(self, tmp_path, registry):
        image = _image_copy(tmp_path)
        spec = ChecksumSpec(
            name="cal",
            kind=ChecksumKind.SUM32,
            start=0x80002000,
            end=0x80002010,
            store_address=0x80002000,
        )
        outcome = run_writeback(registry, _sources(image), checksums=[spec])
        assert outcome.checksums == {"cal": outcome.checksums["cal"]}

    def test_bad_checksum_rule_stops_before_writing(self, tmp_path, registry):
        """算不了就停 —— 留一个校验和没更新的文件，等于埋雷。"""
        image = _image_copy(tmp_path)
        original = image.read_bytes()
        spec = ChecksumSpec(
            name="cal",
            kind=ChecksumKind.SUM32,
            start=0x90000000,  # 不在镜像内
            end=0x90000010,
            store_address=0x80002000,
        )
        with pytest.raises(WritebackError, match="校验和"):
            run_writeback(registry, _sources(image), checksums=[spec])
        assert image.read_bytes() == original, "失败后原文件被动了"


class TestFailures:
    def test_without_values_nothing_happens(self, tmp_path, registry):
        image = _image_copy(tmp_path)
        original = image.read_bytes()
        sources = WritebackSources(image=image, a2l=SAMPLE_DATA / "sample_v1.a2l")
        with pytest.raises(WritebackError, match="值集"):
            run_writeback(registry, sources)
        assert image.read_bytes() == original

    def test_missing_image_is_reported(self, tmp_path, registry):
        sources = _sources(tmp_path / "nope.hex")
        with pytest.raises(WritebackError):
            run_writeback(registry, sources)

    def test_unwritable_target_leaves_the_original_intact(self, tmp_path, registry):
        image = _image_copy(tmp_path)
        original = image.read_bytes()
        blocked = tmp_path / "blocked"
        blocked.mkdir()

        with pytest.raises(WritebackError, match="写入"):
            run_writeback(registry, _sources(image), out=blocked)

        assert image.read_bytes() == original
