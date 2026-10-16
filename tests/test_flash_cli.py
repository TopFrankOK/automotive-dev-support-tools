"""刷写命令行的测试。

命令层刻意保持薄，所以这里测的是**看得见的行为**：

  - 跑一次刷写，逐步输出能被看到
  - 退出码语义分明：成功 0、失败 1、状态不对（没得续 / 拒绝续刷）2
  - 历史记录列得出来，且能按目标过滤

退出码分开是刻意的：脚本调用时需要区分
「刷写失败了」和「根本不该发起这次刷写」。
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from automotive_dev_support.apps.cli.main import app

runner = CliRunner()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """把刷写状态目录指到临时目录，别写进真实的应用数据目录。"""
    path = tmp_path / "flash-state"
    path.mkdir()
    monkeypatch.setenv("ADS_FLASH_STATE_DIR", str(path))
    return path


@pytest.fixture
def image(sample_dir):
    return sample_dir / "sample_v1.hex"


class TestFlashRun:
    def test_runs_and_reports_steps(self, state_dir, image):
        result = runner.invoke(
            app, ["flash", "run", str(image), "--target", "ECM-CLI", "--operator", "zhaolin"]
        )
        assert result.exit_code == 0, result.output
        assert "刷写完成" in result.stdout
        for label in ("进入编程会话", "安全访问", "擦除", "传输", "校验", "复位"):
            assert label in result.stdout

    def test_reports_byte_progress(self, state_dir, image):
        result = runner.invoke(app, ["flash", "run", str(image), "--target", "ECM-CLI"])
        assert result.exit_code == 0, result.output
        assert "字节" in result.stdout

    def test_missing_image_exits_with_2(self, state_dir, tmp_path):
        result = runner.invoke(
            app, ["flash", "run", str(tmp_path / "nope.hex"), "--target", "ECM-CLI"]
        )
        assert result.exit_code == 2
        assert "不存在" in result.output


class TestFlashResume:
    def test_resume_without_checkpoint_exits_with_2(self, state_dir, image):
        result = runner.invoke(app, ["flash", "resume", str(image), "--target", "ECM-CLI"])
        assert result.exit_code == 2
        assert "没有可续的检查点" in result.output


class TestFlashHistory:
    def test_empty_history_is_not_an_error(self, state_dir):
        result = runner.invoke(app, ["flash", "history"])
        assert result.exit_code == 0
        assert "没有记录" in result.stdout

    def test_history_lists_past_runs(self, state_dir, image):
        for target in ("ECM-A", "ECM-B"):
            run = runner.invoke(app, ["flash", "run", str(image), "--target", target])
            assert run.exit_code == 0, run.output
        result = runner.invoke(app, ["flash", "history"])
        assert result.exit_code == 0
        assert "共 2 条记录" in result.stdout, result.stdout
        assert "ECM-A" in result.stdout
        assert "ECM-B" in result.stdout

    def test_history_can_be_filtered_by_target(self, state_dir, image):
        for target in ("ECM-A", "ECM-B"):
            run = runner.invoke(app, ["flash", "run", str(image), "--target", target])
            # 前置必须成功，否则失败信息会是「没有记录」，看不出真正原因
            assert run.exit_code == 0, run.output
        result = runner.invoke(app, ["flash", "history", "--target", "ECM-A"])
        assert "共 1 条记录" in result.stdout, result.stdout
        assert "ECM-B" not in result.stdout
