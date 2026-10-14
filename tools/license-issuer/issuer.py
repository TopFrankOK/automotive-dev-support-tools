#!/usr/bin/env python3
"""Automotive Software Development Support Tools 许可码签发工具 —— 作者专用。

这个脚本**不随软件分发**，只在你自己的机器上跑。

## 三条命令

    issuer.py init                              一次性：生成密钥对
    issuer.py new --code 9F3A-51C7-D2E8-4B06 --name 张三
                                                日常：签发一个月期许可码
    issuer.py renew --code 9F3A-51C7-D2E8-4B06  续期：自动接续到期日

## 设计取向

这个工具的全部意义就是**把签发成本压到 10 秒**。
因为只要签发稍麻烦一点，你迟早会开始「帮他延期几天算了」，
然后授权体系就名存实亡了。

具体做法：
  - 一条命令出码，**自动复制到剪贴板**，你直接 Ctrl+V 发微信
  - `--message` 打印一份可直接发送的完整消息，连措辞都不用想
  - `renew` 不需要你记谁的许可什么时候到期，它自己查记录
  - 每次签发自动落一份 CSV，机器多了也不会乱

## 安全提醒

`keys/private_key.hex` 是整套授权体系的唯一命门。
它已经写进 .gitignore，但请额外做两件事：
  1. 生成后立刻复制到离线 U 盘或密码管理器
  2. 永远不要把它放进任何打包产物
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 让脚本能直接运行（无需先 pip install -e .）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from automotive_dev_support.core.licensing import (  # noqa: E402
    LicenseError,
    decode_unverified,
    generate_keypair,
    issue,
    private_key_from_hex,
)

DEFAULT_KEY_DIR = Path(__file__).resolve().parent / "keys"
PRIVATE_KEY_NAME = "private_key.hex"
PUBLIC_KEY_NAME = "public_key.txt"
RECORDS_NAME = "licenses.csv"
RECORD_FIELDS = [
    "lic_id",
    "licensee",
    "machine",
    "issued_at",
    "expires_at",
    "days",
    "budget_hours",
    "features",
    "code",
    "note",
]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


#: 台账里的时间戳格式。写用第一个（精确到秒，与 `issue()` 的截断一致，
#: 因此往返无损）；读时逐级放宽，兼容更早期只有日期/到分的记录。
_STAMP_WRITE = "%Y-%m-%d %H:%M:%S"
_STAMP_READ = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _format_stamp(moment: datetime) -> str:
    return moment.strftime(_STAMP_WRITE)


def _parse_stamp(text: str) -> datetime | None:
    """解析台账时间戳。

    兼容只有日期的旧记录：存到「日」看似够用，实际会让 `renew`
    把到期时刻拉回零点（`11-13 14:12` 变成 `11-13 00:00`），
    续期结果与原始许可差十几小时 —— 人看不出，但账就不准了。
    """
    for fmt in _STAMP_READ:
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except (AttributeError, ValueError):
            continue
    return None


def _next_lic_id(records: list[dict[str, str]]) -> str:
    year = _now().year
    seq = sum(1 for r in records if r.get("lic_id", "").startswith(f"L-{year}-")) + 1
    return f"L-{year}-{seq:04d}"


def _read_records(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _append_record(path: Path, row: dict[str, str]) -> None:
    """追加一条签发记录。

    若已有文件的表头与本版字段不一致（比如升级后新增了 `code`），
    先把整份记录按新表头重写一遍再追加。
    不做这件事的后果很隐蔽：`csv.DictWriter` 按 `fieldnames` 顺序落值，
    表头与行会错位，整本台账从此对不上号。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_records(path)
    stale_header = path.is_file() and not _header_matches(path)

    if stale_header:
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=RECORD_FIELDS)
            writer.writeheader()
            for old in existing:
                writer.writerow({k: old.get(k, "") for k in RECORD_FIELDS})

    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RECORD_FIELDS)
        if not existing and not stale_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in RECORD_FIELDS})


def _header_matches(path: Path) -> bool:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        head = next(csv.reader(fh), [])
    return head == RECORD_FIELDS


def _write_public_key(path: Path, pub_b64: str) -> None:
    """把公钥落盘。

    只打印不落盘是个陷阱：作者把公钥粘进 config.py 之后，
    过几个月想核对「软件里那串到底是不是我这对密钥的」就没有依据了。
    """
    path.write_text(pub_b64 + "\n", encoding="utf-8")


def _copy_to_clipboard(text: str) -> bool:
    """复制到剪贴板。失败不影响签发 —— 只是少一点方便。"""
    candidates = [
        (["clip"], None),
        (["pbcopy"], None),
        (["xclip", "-selection", "clipboard"], None),
        (["wl-copy"], None),
    ]
    for cmd, _ in candidates:
        try:
            proc = subprocess.run(
                cmd, input=text.encode("utf-8"), capture_output=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    return False


def _print_code_block(code: str) -> None:
    print()
    print("  许可码（已尝试复制到剪贴板）：")
    print()
    print(f"  {code}")
    print()
    print(f"  长度 {len(code)} 字符")


def _wechat_message(payload, code: str) -> str:
    """一份可以直接粘贴进微信的完整消息。

    加这一步是为了消除「我该怎么跟他说」的思考成本 ——
    这类摩擦看起来很小，但它是让人放弃规范流程的主要原因。
    """
    lines = [
        f"【Automotive Software Development Support Tools 许可码 · {payload.licensee}】",
        f"有效期至 {payload.expires_at:%Y-%m-%d}（{payload.grace_days} 天宽限）",
        "",
        "打开软件 → 把下面这一整段复制进激活框（含开头 WF1，不要漏字符）：",
        "",
        code,
        "",
        "提示：到期前一周软件会提醒你，到时把机器码再发我一次即可。",
    ]
    return "\n".join(lines)


def _load_private(key_dir: Path):
    path = key_dir / PRIVATE_KEY_NAME
    if not path.is_file():
        raise SystemExit(
            f"找不到私钥：{path}\n请先运行：python {Path(__file__).name} init"
        )
    return private_key_from_hex(path.read_text("utf-8"))


def _parse_features(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [f.strip() for f in raw.split(",") if f.strip()]


def _parse_fingerprint(raw: str | None) -> dict[str, str]:
    """`board=abc,disk=def` → 字典。留空则只按机器码绑定。"""
    if not raw:
        return {}
    result: dict[str, str] = {}
    for part in raw.split(","):
        key, _, value = part.partition("=")
        if key.strip() and value.strip():
            result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    key_dir: Path = args.key_dir
    path = key_dir / PRIVATE_KEY_NAME
    if path.exists() and not args.force:
        raise SystemExit(
            f"私钥已存在：{path}\n"
            f"重新生成会让**所有已签发的许可码立即失效**。\n"
            f"确实要重建请加 --force。"
        )

    priv_hex, pub_b64 = generate_keypair()
    key_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(priv_hex, encoding="utf-8")
    pub_path = key_dir / PUBLIC_KEY_NAME
    _write_public_key(pub_path, pub_b64)

    print("=" * 72)
    print("  密钥对已生成")
    print("=" * 72)
    print()
    print(f"  私钥：{path}")
    print("        ⚠ 立刻复制到离线 U 盘或密码管理器，然后从工作目录删掉。")
    print("        ⚠ 它一旦泄露，任何人都能自己签发许可码。")
    print()
    print(f"  公钥：{pub_path}（可随软件分发，不含任何秘密）")
    print()
    print("  公钥（base64）：")
    print()
    print(f"  {pub_b64}")
    print()
    print("  下一步：把上面这行公钥粘贴到")
    print("    src/automotive_dev_support/platform/licensing/config.py")
    print("  的 PUBLIC_KEY_B64 = \"...\"，然后重新打包软件。")
    print()
    print("  这一步只需做一次，之后签发的公钥就不再变了。")
    print("=" * 72)
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    key_dir: Path = args.key_dir
    private = _load_private(key_dir)
    records_path = key_dir / RECORDS_NAME
    records = _read_records(records_path)

    lic_id = args.lic_id or _next_lic_id(records)
    machine = args.code.strip().upper()

    code, payload = issue(
        private,
        lic_id=lic_id,
        licensee=args.name,
        machine_code=machine,
        fingerprint=_parse_fingerprint(args.fingerprint),
        days=args.days,
        features=_parse_features(args.features),
        budget_hours=args.hours,
        grace_days=args.grace,
        note=args.note,
    )

    _append_record(
        records_path,
        {
            "lic_id": lic_id,
            "licensee": args.name,
            "machine": machine,
            "issued_at": _format_stamp(payload.issued_at),
            "expires_at": _format_stamp(payload.expires_at),
            "days": str(args.days),
            "budget_hours": "" if args.hours is None else f"{args.hours:g}",
            "features": ",".join(payload.features),
            # 存下许可码本身：同事说「码找不到了再发我一遍」时，
            # 直接从这里捞出来重发即可，不必重新签发污染台账。
            "code": code,
            "note": args.note,
        },
    )

    print(f"  许可证编号：{lic_id}")
    print(f"  被授权人：  {args.name}")
    print(f"  机器码：    {machine}")
    print(f"  有效期至：  {payload.expires_at:%Y-%m-%d}（{args.days} 天）")
    if args.hours is not None:
        print(f"  时长配额：  {args.hours:g} 小时")
    print(f"  已记入：    {records_path}")
    _print_code_block(code)

    if _copy_to_clipboard(code):
        print("  ✓ 已复制到剪贴板")
    else:
        print("  （复制到剪贴板失败，请手动选中复制）")

    if args.message:
        print()
        print("-" * 72)
        print(_wechat_message(payload, code))
        print("-" * 72)
    return 0


def cmd_renew(args: argparse.Namespace) -> int:
    key_dir: Path = args.key_dir
    private = _load_private(key_dir)
    records_path = key_dir / RECORDS_NAME
    records = _read_records(records_path)

    machine = args.code.strip().upper()
    previous = [r for r in records if r.get("machine", "").upper() == machine]
    if not previous:
        print(f"  记录里没有找到机器码 {machine} 的历史签发。")
        print("  将按新许可处理（到期日从现在起算）。")
        base = None
    else:
        last = previous[-1]
        base = _parse_stamp(last.get("expires_at", ""))
        if base:
            print(f"  上次到期：{base:%Y-%m-%d %H:%M}（{last.get('licensee', '')}）")

    now = _now()
    if base and base > now:
        print(f"  从旧到期日接续，不损失剩余 {int((base - now).days)} 天")

    name = args.name or (previous[-1].get("licensee", "") if previous else "")
    lic_id = args.lic_id or _next_lic_id(records)

    # 关键细节：新到期日接在**旧到期日**之后，而不是从今天起算。
    # 同事提前一周来续期，不该损失剩下那 7 天 —— 那是你会被反复追问
    # 的一类小事，不如直接做对。
    #
    # 但 not_before 必须是**现在**：新码要立刻能用，
    # 否则用户会发现「续了期却还是打不开」。
    code, payload = issue(
        private,
        lic_id=lic_id,
        licensee=name,
        machine_code=machine,
        fingerprint=_parse_fingerprint(args.fingerprint),
        days=args.days,
        features=_parse_features(args.features),
        budget_hours=args.hours,
        grace_days=args.grace,
        not_before=now,
        expire_from=base,
        note=args.note or "续期",
    )

    _append_record(
        records_path,
        {
            "lic_id": lic_id,
            "licensee": name,
            "machine": machine,
            "issued_at": _format_stamp(payload.issued_at),
            "expires_at": _format_stamp(payload.expires_at),
            "days": str(args.days),
            "budget_hours": "" if args.hours is None else f"{args.hours:g}",
            "features": ",".join(payload.features),
            "code": code,
            "note": payload.note,
        },
    )

    print(f"  新许可证编号：{lic_id}")
    print(f"  新到期日：    {payload.expires_at:%Y-%m-%d}")
    _print_code_block(code)
    if _copy_to_clipboard(code):
        print("  ✓ 已复制到剪贴板")
    if args.message:
        print()
        print("-" * 72)
        print(_wechat_message(payload, code))
        print("-" * 72)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = _read_records(args.key_dir / RECORDS_NAME)
    if not records:
        print("  还没有任何签发记录。")
        return 0

    now = _now()
    print(f"  {'编号':<12} {'姓名':<8} {'机器码':<20} {'到期':<12} {'剩余':>6}")
    print("  " + "-" * 64)
    # 每台机器只显示最近一条
    latest: dict[str, dict[str, str]] = {}
    for row in records:
        latest[row.get("machine", "")] = row
    for row in sorted(latest.values(), key=lambda r: r.get("expires_at", "")):
        exp = _parse_stamp(row.get("expires_at", ""))
        # 表格里只显示到日 —— 台账存的是精确时刻，但看的人只关心哪天到期
        shown = f"{exp:%Y-%m-%d}" if exp else "?"
        left = f"{(exp - now).days} 天" if exp else "?"
        print(
            f"  {row.get('lic_id', ''):<12} {row.get('licensee', ''):<8} "
            f"{row.get('machine', ''):<20} {shown:<12} {left:>6}"
        )
    print()
    print(f"  共 {len(latest)} 台机器，{len(records)} 条签发记录。")
    print("  续期：issuer.py renew --code <机器码>")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """解析许可码查看内容（**不验签**，仅用于和同事对账）。"""
    try:
        payload = decode_unverified(args.code)
    except LicenseError as exc:
        print(f"  解析失败：{exc}")
        return 1
    print(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issuer",
        description="Automotive Software Development Support Tools 许可码签发工具（作者专用，不随软件分发）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型用法：\n"
            "  issuer.py new --code 9F3A-51C7-D2E8-4B06 --name 张三\n"
            "  issuer.py renew --code 9F3A-51C7-D2E8-4B06\n"
            "  issuer.py list\n"
        ),
    )
    parser.add_argument(
        "--key-dir", type=Path, default=DEFAULT_KEY_DIR, help="密钥与记录目录"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成密钥对（只做一次）")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的私钥")
    p_init.set_defaults(func=cmd_init)

    def add_issue_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--code", required=True, help="同事报给你的机器码")
        p.add_argument("--name", default="", help="被授权人姓名")
        p.add_argument("--days", type=int, default=30, help="有效期天数（默认 30）")
        p.add_argument("--grace", type=int, default=7, help="到期后宽限天数（默认 7）")
        p.add_argument(
            "--hours",
            type=float,
            default=None,
            help="累计运行小时配额（默认不限）。防改系统日期的第二道锁。",
        )
        p.add_argument("--features", default="", help="授权功能项，逗号分隔；留空=全功能")
        p.add_argument("--note", default="", help="备注")
        p.add_argument("--lic-id", default="", help="指定许可证编号")
        p.add_argument(
            "--fingerprint",
            default="",
            help="加强绑定：board=xxx,disk=yyy（默认只按机器码绑定）",
        )
        p.add_argument("--message", action="store_true", help="额外打印可直接发送的消息")

    p_new = sub.add_parser("new", help="签发新许可码")
    add_issue_args(p_new)
    p_new.set_defaults(func=cmd_new)

    p_renew = sub.add_parser("renew", help="为已有机器续期")
    add_issue_args(p_renew)
    p_renew.set_defaults(func=cmd_renew)

    p_list = sub.add_parser("list", help="查看所有机器的到期情况")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="解析许可码内容（不验签）")
    p_show.add_argument("code", help="许可码")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except LicenseError as exc:
        print(f"  错误：{exc}")
        if exc.hint:
            print(f"  提示：{exc.hint}")
        return 2
    except KeyboardInterrupt:
        print("\n  已取消。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
