"""HTTP 服务层的测试。

**这里不重复 services 的测试** —— 用例逻辑已在各自的测试里守过。
这里只测「HTTP 边界」这一层自己的事：

  - 参数怎么进、JSON 怎么出（字段名别悄悄变，前端靠它）
  - 输入有问题时状态码是 400 还是 500（400 才是「你的输入不对」）
  - 刷写会落审计（P3 的两条硬语义不能因为换了个入口就丢）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automotive_dev_support.apps.api.main import app

client = TestClient(app)

A2L = "tests/data/sample_v1.a2l"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """刷写状态目录指到临时目录 —— 测试不许写进真实的应用数据目录。"""
    path = tmp_path / "flash-state"
    monkeypatch.setenv("ADS_FLASH_STATE_DIR", str(path))
    return path


class TestPlugins:
    def test_health(self):
        assert client.get("/api/health").json()["ok"] is True

    def test_lists_every_plugin(self):
        data = client.get("/api/plugins").json()
        assert data["summary"]["total"] >= 13
        assert data["summary"]["usable"] == data["summary"]["total"]
        ids = {p["id"] for p in data["plugins"]}
        assert {"writer-hex", "writer-s19", "flash-recipe-sim"} <= ids


class TestCompare:
    def test_compares_two_samples(self):
        res = client.post(
            "/api/compare",
            json={
                "baseline": {"a2l": A2L, "values": "tests/data/sample_v1.cdfx"},
                "candidate": {
                    "a2l": "tests/data/sample_v2.a2l",
                    "values": "tests/data/sample_v2.cdfx",
                },
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["summary"]["meaningful"] >= 1
        assert any(c["path"] == "Kp_Speed" for c in data["changes"])

    def test_missing_file_is_a_400_not_a_500(self):
        """输入不对是 400 —— 500 会被当成服务坏了。"""
        res = client.post(
            "/api/compare",
            json={
                "baseline": {"a2l": "tests/data/nope.a2l"},
                "candidate": {"a2l": "tests/data/nope.a2l"},
            },
        )
        assert res.status_code == 400


class TestMeasurement:
    def test_lists_signals(self):
        data = client.get(
            "/api/measurement/signals", params={"file": "tests/data/sample_v1.mf4"}
        ).json()
        assert any(s["name"] == "EngineSpeed" for s in data["signals"])

    def test_trend_returns_bands(self):
        data = client.post(
            "/api/trend",
            json={
                "file": "tests/data/sample_v1.mf4",
                "signal": "EngineSpeed",
                "points": 200,
            },
        ).json()
        assert data["bands"]
        assert data["sample_count"] > 0

    def test_unknown_signal_is_a_400(self):
        res = client.post(
            "/api/trend",
            json={"file": "tests/data/sample_v1.mf4", "signal": "NoSuchSignal"},
        )
        assert res.status_code == 400


class TestWriteback:
    def test_writeback_is_lossless(self, tmp_path):
        """HTTP 这条路上，无损同样必须成立。"""
        out = tmp_path / "out.hex"
        res = client.post(
            "/api/writeback",
            json={
                "image": "tests/data/sample_v1.hex",
                "a2l": A2L,
                "values": "tests/data/sample_v1.cdfx",
                "out": str(out),
            },
        )
        assert res.status_code == 200, res.text
        assert out.read_bytes() == Path("tests/data/sample_v1.hex").read_bytes()


class TestFlash:
    def test_run_keeps_the_p3_semantics(self, state_dir):
        """换入口不能丢语义：步骤要逐步报出来，成功要留审计。"""
        res = client.post(
            "/api/flash/run",
            json={"image": "tests/data/sample_v1.hex", "operator": "pytest"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["ok"], data["error"]
        assert [s["index"] for s in data["steps"]] == [0, 1, 2, 3, 4, 5]

        history = client.get("/api/flash/history").json()["records"]
        assert history[-1]["operator"] == "pytest"
        assert history[-1]["success"] is True

    def test_history_can_be_filtered_by_target(self, state_dir):
        client.post(
            "/api/flash/run",
            json={"image": "tests/data/sample_v1.hex", "target_id": "ECM-API"},
        )
        all_records = client.get("/api/flash/history").json()["records"]
        filtered = client.get(
            "/api/flash/history", params={"target": "ECM-API"}
        ).json()["records"]
        assert filtered
        assert all(r["target"] == "ECM-API" for r in filtered)
        assert len(filtered) <= len(all_records)
