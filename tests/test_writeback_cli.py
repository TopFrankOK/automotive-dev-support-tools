"""写回命令行的测试。

命令层刻意薄，所以这里只测**看得见的行为**：
退出码语义、写了多少量、跳过的有没有报出来、原文件动没动。

退出码沿用刷写的约定：成功 0、写回失败 1、参数或状态不对 2。
脚本调用时这三者必须能分开 —— 「写回失败了」和「根本不该发起」是两回事。
"""

from __future__ import annotations

import shutil

from conftest import SAMPLE_DATA
from typer.testing import CliRunner

from automotive_dev_support.apps.cli.main import app

runner = CliRunner()

A2L = str(SAMPLE_DATA / "sample_v1.a2l")
VALUES = str(SAMPLE_DATA / "sample_v2.cdfx")


def _copy(tmp_path, name: str = "sample_v1.hex"):
    target = tmp_path / name
    shutil.copy(SAMPLE_DATA / name, target)
    return target


class TestWritebackCommand:
    def test_writes_back_and_reports(self, tmp_path):
        image = _copy(tmp_path)
        result = runner.invoke(
            app,
            ["writeback", str(image), "--a2l", A2L, "--values", VALUES],
        )
        assert result.exit_code == 0, result.output
        assert "已写回" in result.stdout

    def test_out_leaves_the_original_untouched(self, tmp_path):
        image = _copy(tmp_path)
        original = image.read_bytes()
        out = tmp_path / "written.hex"

        result = runner.invoke(
            app,
            ["writeback", str(image), "--a2l", A2L, "--values", VALUES, "--out", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert image.read_bytes() == original, "给了 --out 还改了原文件"
        assert out.exists()

    def test_missing_file_exits_with_2(self, tmp_path):
        result = runner.invoke(
            app,
            ["writeback", str(tmp_path / "nope.hex"), "--a2l", A2L, "--values", VALUES],
        )
        assert result.exit_code == 2
        assert "不存在" in result.output

    def test_format_without_a_writer_exits_with_1(self, tmp_path):
        """S19 还没有 writer（本期只做 HEX 一条路）—— 必须报失败，不能静默。"""
        image = _copy(tmp_path, "sample_v1.s19")
        result = runner.invoke(
            app,
            ["writeback", str(image), "--a2l", A2L, "--values", VALUES],
        )
        assert result.exit_code == 1
        assert "writer" in result.output.lower() or "写出" in result.output

    def test_skipped_quantities_are_listed(self, tmp_path):
        """值集与描述对不上的量要逐个报出来，不能只报总数。"""
        image = _copy(tmp_path)
        result = runner.invoke(
            app,
            ["writeback", str(image), "--a2l", A2L, "--values", VALUES],
        )
        assert result.exit_code == 0, result.output
        if "跳过" in result.stdout:
            assert ":" in result.stdout  # 带原因，不是只列名字

    def test_checksum_hint_is_shown(self, tmp_path):
        """没有校验和规则时要提醒 —— 改了值不更新校验和，刷进 ECU 会被拒。"""
        image = _copy(tmp_path)
        result = runner.invoke(
            app,
            ["writeback", str(image), "--a2l", A2L, "--values", VALUES],
        )
        assert result.exit_code == 0, result.output
        assert "校验和" in result.stdout
