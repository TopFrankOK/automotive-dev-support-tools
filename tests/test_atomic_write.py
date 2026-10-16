"""原子落盘的测试。

**为什么值得单独测**：`os.replace` 在 Windows 上会偶发 `PermissionError`
—— 刚写完的 `.tmp` 文件被杀毒软件的实时扫描短暂占用。
症状是「偶尔失败，重跑就好」，于是永远没人真正去修它。

这里用 monkeypatch 把「被占用」变成确定性事件，
这样重试逻辑就是被测试过的，而不是靠运气。
"""

from __future__ import annotations

import os

import pytest

from automotive_dev_support.platform import atomic
from automotive_dev_support.platform.atomic import replace_atomically


def test_replaces_the_target(tmp_path):
    target = tmp_path / "data.json"
    target.write_text("旧", encoding="utf-8")
    tmp = tmp_path / "data.json.tmp"
    tmp.write_text("新", encoding="utf-8")

    replace_atomically(tmp, target)

    assert target.read_text(encoding="utf-8") == "新"
    assert not tmp.exists()


def test_retries_when_the_tmp_file_is_locked(tmp_path, monkeypatch):
    """第一次被占用、第二次成功 —— 必须重试而不是直接失败。"""
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "拒绝访问")
        return real(src, dst, **kwargs)

    monkeypatch.setattr(atomic.os, "replace", flaky)
    monkeypatch.setattr(atomic, "_DELAY_S", 0)

    target = tmp_path / "data.json"
    tmp = tmp_path / "data.json.tmp"
    tmp.write_text("新", encoding="utf-8")

    replace_atomically(tmp, target)

    assert calls["n"] == 2
    assert target.read_text(encoding="utf-8") == "新"


def test_gives_up_after_several_attempts(tmp_path, monkeypatch):
    """一直被占用就要抛出来 —— 无限重试会把一次故障变成卡死。"""
    calls = {"n": 0}

    def always_locked(src, dst, **kwargs):
        calls["n"] += 1
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(atomic.os, "replace", always_locked)
    monkeypatch.setattr(atomic, "_DELAY_S", 0)

    target = tmp_path / "data.json"
    tmp = tmp_path / "data.json.tmp"
    tmp.write_text("新", encoding="utf-8")

    with pytest.raises(PermissionError):
        replace_atomically(tmp, target)

    assert calls["n"] == atomic._ATTEMPTS


def test_other_os_errors_are_not_retried(tmp_path, monkeypatch):
    """磁盘满、路径不存在这类错误，重试多少次都不会变好。"""
    calls = {"n": 0}

    def broken(src, dst, **kwargs):
        calls["n"] += 1
        raise OSError(28, "设备上没有空间")

    monkeypatch.setattr(atomic.os, "replace", broken)
    monkeypatch.setattr(atomic, "_DELAY_S", 0)

    tmp = tmp_path / "data.json.tmp"
    tmp.write_text("新", encoding="utf-8")

    with pytest.raises(OSError):
        replace_atomically(tmp, tmp_path / "data.json")

    assert calls["n"] == 1
