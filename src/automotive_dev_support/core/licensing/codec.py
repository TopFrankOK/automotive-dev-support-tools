"""许可码的编解码。

## 为什么是「许可码」而不是「许可证文件」

Ed25519 签名只有 64 字节。整个许可码紧凑编码后大约 400–500 个字符，
**一条微信消息就能发**。这消灭了一整类摩擦：
不用解释放到哪个目录、不用处理路径、不用教同事怎么导入。
他复制，粘贴，完事。

## 为什么用 zlib

JSON 里字段名重复度很高（`"expires_at"` 之类），压缩后能省掉约三成长度。
压缩同时带来一个副作用：许可码看起来是乱码，
用户不会忍不住去手改里面的日期 —— 这算额外收益。

## 为什么载荷完全公开

安全性来自签名，不来自保密。载荷即使被完整解出来也无所谓，
因为攻击者改不动它 —— 改一个字节签名就失效。
反过来，如果试图混淆或加密载荷，只会让调试变难、让自己也读不出来。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib

from automotive_dev_support.core.licensing.errors import MalformedCode
from automotive_dev_support.core.licensing.payload import LicensePayload

__all__ = ["encode", "decode", "decode_unverified", "normalize", "PREFIX"]

PREFIX = "WF1"
_SEP = "."
_MAX_PAYLOAD_BYTES = 256 * 1024
"""解压后的大小上限。防止构造出来的压缩炸弹把内存打爆。"""

_WHITESPACE = re.compile(r"\s+")


def normalize(code: str) -> str:
    """去掉所有空白与换行。

    **这一步必须存在。** 微信转发、手机复制、邮件换行都很容易掺进
    空白字符。如果严格比对，作者会被「我粘贴了没反应」的求助淹没。
    """
    return _WHITESPACE.sub("", code or "").strip()


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise MalformedCode("许可码编码损坏") from exc


def canonical_bytes(payload: LicensePayload) -> bytes:
    """签名的输入：规范化 JSON 字节。

    必须 `sort_keys=True` —— 否则字典序不固定，
    签发与校验算出的字节不一致，会出现「明明没改却验签失败」的诡异 bug。
    """
    dumped = payload.model_dump(mode="json")
    text = json.dumps(dumped, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return text.encode("utf-8")


def encode(payload: LicensePayload, signature: bytes) -> str:
    """装配许可码。"""
    blob = canonical_bytes(payload)
    return _SEP.join(
        (PREFIX, _b64u_encode(zlib.compress(blob, 9)), _b64u_encode(signature))
    )


def _inflate(raw: bytes) -> bytes:
    """带解压后大小上限的 inflate。

    注意 `zlib.decompress(data, n)` 的第二个参数是 `wbits`（8–15），
    不是长度上限 —— 想限制输出大小只能用 `decompressobj`。
    `max_length` 截断后必须检查 `unconsumed_tail`，否则压缩炸弹会被
    悄悄截断成半截 JSON，报出难以定位的解析错误。
    """
    engine = zlib.decompressobj()
    try:
        blob = engine.decompress(raw, _MAX_PAYLOAD_BYTES)
    except zlib.error as exc:
        raise MalformedCode("许可码内容损坏，可能复制时漏了字符") from exc
    if engine.unconsumed_tail or not engine.eof:
        raise MalformedCode("许可码内容异常膨胀，已拒绝解析")
    return blob


def _split(code: str) -> tuple[bytes, bytes]:
    parts = normalize(code).split(_SEP)
    if len(parts) != 3 or parts[0] != PREFIX:
        raise MalformedCode("许可码格式不正确，应以 WF1 开头并由三段组成")

    return _inflate(_b64u_decode(parts[1])), _b64u_decode(parts[2])


def decode_unverified(code: str) -> LicensePayload:
    """只解析载荷，**不做任何验签**。

    用途：即使机器码不匹配，也要能在界面上显示
    「这张许可码是签给谁的、什么时候到期」，
    这样用户和作者才能对上号、定位问题。
    """
    blob, _ = _split(code)
    return _load_payload(blob)


def decode(code: str) -> tuple[LicensePayload, bytes, bytes]:
    """解析出 (载荷, 签名, 被签名的原始字节)。

    返回被签名的原始字节，是为了让校验方对**完全相同的字节**验签，
    避免重新序列化引入的差异。
    """
    blob, signature = _split(code)
    return _load_payload(blob), signature, blob


def _load_payload(blob: bytes) -> LicensePayload:
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedCode("许可码内容无法解析") from exc
    try:
        return LicensePayload.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise MalformedCode(f"许可码字段缺失或非法：{exc}") from exc
