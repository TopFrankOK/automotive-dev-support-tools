"""测试基建。

`REPO_ROOT` 用于定位插件目录；`MACHINE_A/B` 是模拟的两台机器指纹，
让授权测试完全脱离真实硬件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "src" / "automotive_dev_support" / "plugins"
SAMPLE_DATA = REPO_ROOT / "tests" / "data"


#: 模拟机器 A / B 的指纹。授权测试不需要真实硬件。
MACHINE_A = {
    "board": "aaaaaaaaaaaaaaaa",
    "disk": "bbbbbbbbbbbbbbbb",
    "sys": "cccccccccccccccc",
}
MACHINE_B = {
    "board": "1111111111111111",
    "disk": "2222222222222222",
    "sys": "3333333333333333",
}
CODE_A = "9F3A-51C7-D2E8-4B06"
CODE_B = "1111-2222-3333-4444"


@pytest.fixture
def utcnow() -> datetime:
    return datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE_DATA
