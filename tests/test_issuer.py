"""签发工具的测试 —— 覆盖作者每天要跑的那条路径。

**为什么一个「工具脚本」值得单测？**

因为它是整套授权体系的单点：签发端一旦出错（码签不出来、
或签出来的码自己软件不认），作者没有任何替代手段 ——
最坏情况下只能临时「手工做个不限期的版本」，授权就废了。

`init / new / renew` 三条命令各覆盖一次，并且每个签出的码
都要**真的用内置校验器验一遍**，而不是只看打印出来的日期对不对。
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import CODE_A, REPO_ROOT
from automotive_dev_support.core.licensing import LicenseVerifier, LocalState, decode_unverified

_ISSUER_PATH = REPO_ROOT / "tools" / "license-issuer" / "issuer.py"


def _load_issuer():
    """把签发脚本当模块加载。它不是包的一部分，只能按路径加载。"""
    spec = importlib.util.spec_from_file_location("wf_issuer", _ISSUER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["wf_issuer"] = module
    spec.loader.exec_module(module)
    return module


issuer = _load_issuer()


@pytest.fixture
def key_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def inited(key_dir: Path, capsys) -> Path:
    """跑过一次 init 的密钥目录。"""
    assert issuer.main(["--key-dir", str(key_dir), "init"]) == 0
    capsys.readouterr()
    return key_dir


def _records(key_dir: Path) -> list[dict[str, str]]:
    with (key_dir / issuer.RECORDS_NAME).open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _public_key(key_dir: Path) -> str:
    return (key_dir / issuer.PUBLIC_KEY_NAME).read_text("utf-8").strip()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_writes_private_and_public_key(inited: Path, capsys):
    """公钥必须落盘。

    只打印不落盘是个陷阱：作者把公钥粘进 config.py 之后，
    过几个月想核对「软件里那串到底是不是我这对密钥的」就没有依据了。
    """
    priv = (inited / issuer.PRIVATE_KEY_NAME).read_text("utf-8").strip()
    pub = _public_key(inited)
    assert len(bytes.fromhex(priv)) == 32
    assert len(pub) > 40

    # 落盘的公钥必须真的能用来校验，而不是随便写了串东西
    LicenseVerifier(pub)


def test_init_refuses_second_run_without_force(inited: Path):
    """重建密钥会让**所有已签发的许可码立即失效**，必须挡一道。"""
    with pytest.raises(SystemExit) as exc:
        issuer.main(["--key-dir", str(inited), "init"])
    assert "--force" in str(exc.value)


def test_init_force_replaces_key(inited: Path, capsys):
    before = _public_key(inited)
    capsys.readouterr()
    assert issuer.main(["--key-dir", str(inited), "init", "--force"]) == 0
    assert _public_key(inited) != before


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


def test_new_issues_verifiable_code(inited: Path, capsys):
    """签出来的码必须能被内置校验器认下来 —— 这是唯一的验收标准。"""
    assert issuer.main(
        ["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"]
    ) == 0
    out = capsys.readouterr().out
    code = _extract_code(out)

    outcome = LicenseVerifier(_public_key(inited)).verify(
        code, LocalState(machine_code=CODE_A)
    )
    assert outcome.state == "valid"
    assert outcome.payload.licensee == "张三"
    assert 29 < outcome.days_left <= 30


def test_new_records_the_code_itself(inited: Path, capsys):
    """台账要存下许可码本身。

    同事说「码找不到了，再发我一遍」时，从台账捞出来重发即可。
    不存的话只能重新签发，lic_id 会变、台账会脏。
    """
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    capsys.readouterr()

    rows = _records(inited)
    assert len(rows) == 1
    assert rows[0]["code"].startswith("WF1.")
    assert decode_unverified(rows[0]["code"]).licensee == "张三"


def test_new_code_is_message_sized(inited: Path, capsys):
    """一条微信能发出去是这套方案成立的前提。"""
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "李四"])
    code = _extract_code(capsys.readouterr().out)
    assert len(code) < 800


def test_new_binds_machine_code(inited: Path, capsys):
    """拷走安装包没关系，拷走许可码不行。"""
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    code = _extract_code(capsys.readouterr().out)

    verifier = LicenseVerifier(_public_key(inited))
    with pytest.raises(Exception) as exc:
        verifier.verify(code, LocalState(machine_code="0000-0000-0000-0000"))
    assert type(exc.value).__name__ == "MachineMismatch"


def test_new_assigns_sequential_ids(inited: Path, capsys):
    for i in range(3):
        issuer.main(
            ["--key-dir", str(inited), "new", "--code", f"{i}{i}{i}{i}-0000-0000-0000",
             "--name", f"同事{i}"]
        )
    capsys.readouterr()
    ids = [r["lic_id"] for r in _records(inited)]
    assert ids == ["L-2026-0001", "L-2026-0002", "L-2026-0003"]


# ---------------------------------------------------------------------------
# renew
# ---------------------------------------------------------------------------


def test_renew_continues_from_previous_expiry(inited: Path, capsys):
    """提前一周来续期不该损失剩下那几天 —— 否则你会被反复追问。"""
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    first = _extract_code(capsys.readouterr().out)
    first_payload = decode_unverified(first)

    issuer.main(["--key-dir", str(inited), "renew", "--code", CODE_A, "--days", "30"])
    renewed = _extract_code(capsys.readouterr().out)
    renewed_payload = decode_unverified(renewed)

    assert renewed_payload.expires_at == first_payload.expires_at + timedelta(days=30)
    # 新码必须**立刻可用**，否则用户会说「续了期还是打不开」
    assert renewed_payload.lic_id != first_payload.lic_id
    outcome = LicenseVerifier(_public_key(inited)).verify(
        renewed, LocalState(machine_code=CODE_A)
    )
    assert outcome.state == "valid"


def test_renew_unknown_machine_starts_from_now(inited: Path, capsys):
    issuer.main(["--key-dir", str(inited), "renew", "--code", CODE_A, "--name", "新人"])
    code = _extract_code(capsys.readouterr().out)
    payload = decode_unverified(code)
    assert abs(payload.days_left(datetime.now(timezone.utc)) - 30) < 1


def test_renew_keeps_licensee_from_records(inited: Path, capsys):
    """续期时不必再报一次姓名 —— 工具自己查台账。"""
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    capsys.readouterr()
    issuer.main(["--key-dir", str(inited), "renew", "--code", CODE_A])
    code = _extract_code(capsys.readouterr().out)
    assert decode_unverified(code).licensee == "张三"


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def test_record_schema_upgrade_preserves_old_rows(inited: Path, capsys):
    """表头升级不能把台账弄错位。

    这是很隐蔽的一类损坏：`csv.DictWriter` 按 `fieldnames` 顺序落值，
    若旧文件表头少一列，新写入的行会整体错位，账目从此对不上号。
    """
    path = inited / issuer.RECORDS_NAME
    # 手工写一份「旧版」台账：字段少一个，且没有 code 列
    old_fields = [f for f in issuer.RECORD_FIELDS if f != "code"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow(
            {"lic_id": "L-2025-0001", "licensee": "老王", "machine": CODE_A,
             "issued_at": "2025-01-01 09:00", "expires_at": "2025-01-31",
             "days": "30", "budget_hours": "", "features": "", "note": ""}
        )
    capsys.readouterr()

    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    capsys.readouterr()

    rows = _records(inited)
    assert len(rows) == 2
    assert rows[0]["licensee"] == "老王"  # 旧记录原样保留
    assert rows[0]["code"] == ""          # 缺的列补空
    assert rows[1]["licensee"] == "张三"
    assert rows[1]["code"].startswith("WF1.")


def test_list_shows_only_latest_per_machine(inited: Path, capsys):
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    issuer.main(["--key-dir", str(inited), "renew", "--code", CODE_A])
    capsys.readouterr()

    assert issuer.main(["--key-dir", str(inited), "list"]) == 0
    out = capsys.readouterr().out
    assert "张三" in out
    assert "共 1 台机器，2 条签发记录" in out


def test_show_reads_code_content_without_verifying(inited: Path, capsys):
    """即使验签会失败，也要能读出「这码是给谁的」。"""
    issuer.main(["--key-dir", str(inited), "new", "--code", CODE_A, "--name", "张三"])
    code = _extract_code(capsys.readouterr().out)

    assert issuer.main(["--key-dir", str(inited), "show", code]) == 0
    out = capsys.readouterr().out
    assert "张三" in out
    assert CODE_A in out


def test_show_rejects_garbage(inited: Path, capsys):
    assert issuer.main(["--key-dir", str(inited), "show", "这不是许可码"]) == 1
    assert "解析失败" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _extract_code(output: str) -> str:
    """从命令输出里把许可码抠出来。

    这样做是刻意的：如果直接调内部函数拿返回值，
    「码有没有真的打印到屏幕上」这件事就测不到了 ——
    而作者正是靠复制屏幕上的那串字符干活。
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("WF1."):
            return stripped
    raise AssertionError(f"输出里没有找到许可码：\n{output}")
