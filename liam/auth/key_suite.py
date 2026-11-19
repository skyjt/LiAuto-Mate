"""签名身份派生（keySuite → formatDK）—— 移植自 ha-lixiang key_suite.py（MIT）。

原项目：https://github.com/C3H3-AI/ha-lixiang（2026-10-10 Phase A 实测定案）。
xdev / hac_key / key_id 是【每设备运行时身份】：
    · xdev    = 登录 device_id（浏览器登录产生，或派生时新建）
    · hac_key = 服务端下发 hmacKey，经 RSA 解密 + AES-CTR 派生
    · key_id  = 服务端按 (KID, xdev) 下发的 keyId
三条实测结论（bootstrap 无鸡生蛋）：
    1. keySuite 的 x-chj 引导签名用【随机 hac】即可通过（服务端不校验）
    2. 派生对 (KID, xdev) 确定性（同参重派结果一致）
    3. 派生身份调业务接口全 code:0（ha-lixiang 验证；本项目 11-19 复验）

livis → 理想同学 KID + App 级 RSA 私钥（App 内置公共常量，非用户私有凭据）。
本模块只做 HTTP + 密码学运算，同步阻塞 —— async 调用方须放 executor/线程。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _pysecrets
import time
import urllib.error
import urllib.request
import uuid
import logging

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_private_key

from .constants import API_APP, EP_KEY_SUITE, SIGN_APP_VERSION

_APP_LIVIS = "livis"

# ---------- 设备 KID ----------
LIVIS_DEVICE_KEY_ID = "02003dbe871b110a15088600003ddfea"  # 理想同学

# ---------- App 级 RSA 私钥（base64 DER，App 内置公共常量） ----------
LIVIS_RSA_PRIVATE_DER_B64 = (
    (
    "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCk2YXvhCI6oVc9nN9R9ay5HaNJJINASSHXvgwc"
    "eBIRSkV/Z/kY4KsiyMNHUAwIjs8zG23Bpzt+QoKbEz6M9xtwofXjgpm5iLcdeCkvcM7ybF3mrDe4fpWFXTjkyIgh"
    "Tv7drLNgkJ5Iav1ZNxLicYw0dfx8py+glF6EGb1pjEmRDWvel9yZPReVwcYd2j0H6OcayCEtF1ztrEUSoOL0Z1aW"
    "Z16y6MueuulNHbeXy2EIYs4GA1gANHDvhNoAfNzLHYRk8dvJ9tRAzBQwasQBVDm6IHLessIDafNz1RDzz3y3kowj"
    "bx8LlJ2JCw17DgbAOKQ0PkUXRJVYRIAIK0hYRBW/AgMBAAECggEAdgTBDZvUgZMWiSaw/tVaxeDBENFSIgj5cKI/"
    "u3X+wWAh5zfBrxzRiIKgw4I8SzgqgNVHO5gFULw/EtSxOGyEuZtKFYpfkeOd7TwkiDFEB2yrwURUVAJT+3mlDK3A"
    "P9B1SLCmbyC6IPBv1ppGK9XM4ZYCoB91SopOLFbdMx5bYLtJZ0Ax7BeKBHuvFi1h6EFkxp5y5AQjshLqgfafPl9j"
    "I8BxNPUqNsjwMTKFXJinPQG5+WGkA2umDGMlLTl+Vuvd7UxgLOSCPM2fanJK/8NZKB2gyysLcgPO92fpB7AyRDMm"
    "bpuoObO3j0nLxl5fMAd1m46CXLmsiYn3M1AP8Yt4GQKBgQDqU9aeybB2bKkzrK3nqymTG3UI6iN9A9i79uH20UKh"
    "YF3lLiDMPc8jviEDZpHG4Zk/alNbbRJrltUfM2wIgIV5Zwes3qLsSbuHtmN2FeWJKbZjsiFKDRnNeaozMXJ5Fena"
    "VK0YVbBK/rQNAl+pSRaVjErScUii6usL7oxNnqT3AwKBgQC0GKmFuNfw5ydh7oFYoe41LNbOEL/3QP8yzuIoUb81"
    "Z23NwURLaqjUXxVJWR2DtN4F/rQTnbFINkzjKJs9Rw083Fi1CXQGr/RgivokPUYOx4ohojGypLVmPUNeIpquN8Bq"
    "Kz1+6K255FUpqjeeV84uTrViReOgkQIsAtcDZxEblQKBgQDE43DftqP4vVBmRN9SWvTx0A5EUUdEUakYNlai5i1Q"
    "HwKGAH46XmzfoW9nxhUSwJfdOt+TYFAr6m5kavaJJkQAP9upGuBWHZXecBeeLsPQviWsGw3xhJR7m5CwtwlySEFX"
    "2/IdElKwkNaEX8w1F15MhbaQn/LiQPUB74wf4/7ENQKBgCBmJ2klHcP68bzOeXqGdyId1O7xWHeUu9RaH5l9S1bC"
    "KqDPWgfvQjwiduPhIkwlZ6PQdHjq74+8JQzgqzzU4W7HfTXkY3kogmAz4FhQpZ/XCeSPFz26H+AquUngE8+vu+/d"
    "o4yHM2mzyBZcxvC3fyIZiswJIrAqJifgwumbyxoZAoGBAJ2EJIUevSSyhNepAa3ABopulEnVqeIaKYED4zGSBk9u"
    "hs+1c3Er74SqcKwGGyM9F8Z9OhSVSS+pdHKNO2ky+tgOlL7Y7pNnUjunhE4BFOffiRYwhVFZroPxCUHx8tQ0rGDG"
    "jz1KZRi09ZW9duseiQpnGkwV2jQH/xtuMrm8jWAh"
)
)


class IdentityDeriveError(RuntimeError):
    """签名身份派生失败（keySuite 非 0 / 网络错误 / 响应缺字段）。"""


def _b64decode(s: str) -> bytes:
    """容错 base64 解码（自动补 `=`）。"""
    return base64.b64decode(str(s) + "=" * (-len(str(s)) % 4))


def _load_private(app_type: str):
    """按身份加载 RSA 私钥。"""
    if app_type == _APP_LIVIS:
        return load_der_private_key(
            base64.b64decode(LIVIS_RSA_PRIVATE_DER_B64), password=None)
    raise IdentityDeriveError(f"未知的 app_type: {app_type!r}（本项目仅支持 livis）")


def _kid_of(app_type: str) -> str:
    if app_type == _APP_LIVIS:
        return LIVIS_DEVICE_KEY_ID
    raise IdentityDeriveError(f"未知的 app_type: {app_type!r}（本项目仅支持 livis）")


def build_data_to_sign(request_id: str, xdev: str, kid: str,
                       nonce: str, ts: str) -> str:
    """keySuite body RSA 签名原文：requestId:deviceId:deviceKeyId:nonce:timestamp。"""
    return ":".join([request_id, xdev, kid, nonce, ts])


def build_key_suite_request(
    xdev: str,
    kid: str,
    priv,
    bearer: str,
    request_id: str,
    nonce: str,
    ts: str,
    boot_hac_hex: str,
    ver: str = SIGN_APP_VERSION,
) -> tuple[bytes, dict[str, str]]:
    """纯函数：组装 keySuite 请求（body + headers），不发网络。

    boot_hac_hex：引导签名用的 hac（32B hex）。实测随机值即可通过
    （全新装机没有 hac —— 服务端不校验该引导签名）。
    """
    data_to_sign = build_data_to_sign(request_id, xdev, kid, nonce, ts)
    sign = base64.b64encode(
        priv.sign(data_to_sign.encode(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    # body 按 Gson 字母序：deviceId, deviceKeyId, nonce, requestId, sign, timestamp
    body = (
        '{"deviceId":"%s","deviceKeyId":"%s","nonce":"%s",'
        '"requestId":"%s","sign":"%s","timestamp":"%s"}'
        % (xdev, kid, nonce, request_id, sign, ts)
    ).encode()
    md5b = base64.b64encode(hashlib.md5(body).digest()).decode()
    # x-chj 11 段 HMAC（\n 拼接 + 末尾 \n，key = 引导 hac 原始 32 字节，
    # 第 3 段/头 X-CHJ-Key = hac 前 32 hex —— 与 keysuite 实测配方一致）
    dtsx = "\n".join([
        "prod", ver, boot_hac_hex[:32], xdev, "POST", "*/*", "zh-Hans-CN",
        md5b, "application/json", ts, nonce.upper() + ts,
    ])
    xsign = hmac.new(
        bytes.fromhex(boot_hac_hex), (dtsx + "\n").encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "Authorization": "Bearer " + bearer,
        "Content-Type": "application/json",
        "Content-MD5": md5b,
        "Accept": "*/*",
        "Content-Language": "zh-Hans-CN",
        "X-CHJ-Timestamp": ts,
        "X-CHJ-Nonce": nonce.upper(),
        "X-CHJ-Sign": xsign,
        "X-CHJ-Deviceid": xdev,
        "X-CHJ-Key": boot_hac_hex[:32],
        "X-CHJ-APP-Version": ver,
        "X-CHJ-Env": "prod",
        "X-CHJ-Version": ver,
    }
    return body, headers


def format_dk(temp_aes_key_b64: str, temp_iv_b64: str,
              hmac_key_b64: str, priv) -> bytes:
    """纯函数：keySuite 响应 → 32B hac_key。

    RD   = RSA-PKCS1v15 解密 tempAesKey
    hac  = AES-256-CTR(key=RD, iv=tempIv).decrypt(hmacKey)
    （2026-10-10 v2/v4 实测配方，与 SO 逆向 §60.1 一致）
    """
    ta = _b64decode(temp_aes_key_b64)
    ti = _b64decode(temp_iv_b64)
    hk = _b64decode(hmac_key_b64)
    rd = priv.decrypt(ta, padding.PKCS1v15())
    dec = Cipher(algorithms.AES(rd), modes.CTR(ti)).decryptor()
    hac = dec.update(hk) + dec.finalize()
    if len(hac) != 32:
        raise IdentityDeriveError(f"formatDK 派生长度异常: {len(hac)} 字节")
    return hac


def derive_identity(app_type: str, xdev: str, bearer: str,
                    timeout: int = 20) -> tuple[str, str]:
    """现场派生签名身份 → (hac_key_hex, key_id)。

    流程：随机 hac 引导签 keySuite → 响应 → formatDK 派生 hac → 取响应 keyId。
    同步阻塞，必须在 executor 线程中调用。
    失败抛 IdentityDeriveError（不回退、不静默 —— 由 config_flow abort 呈现）。
    """
    kid = _kid_of(app_type)
    priv = _load_private(app_type)
    ts = str(int(time.time() * 1000))
    request_id = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    boot_hac = _pysecrets.token_hex(32)   # 引导签名（V4d：随机值即通过）

    body, headers = build_key_suite_request(
        xdev, kid, priv, bearer, request_id, nonce, ts, boot_hac)
    req = urllib.request.Request(
        API_APP + EP_KEY_SUITE, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as err:
        raise IdentityDeriveError(
            f"keySuite HTTP {err.code}: {err.read().decode()[:200]}") from err
    except urllib.error.URLError as err:
        raise IdentityDeriveError(f"keySuite 网络错误: {err.reason}") from err

    try:
        j = json.loads(raw)
    except json.JSONDecodeError as err:
        raise IdentityDeriveError(f"keySuite 响应非 JSON: {raw[:160]}") from err
    if j.get("code") != 0:
        raise IdentityDeriveError(
            f"keySuite code={j.get('code')} msg={j.get('msg') or j.get('message') or ''}")

    dd = j.get("data") or {}
    ks = dd.get("keySuite") or {}
    sec = ks.get("keySecret") or {}
    for field, value in (("tempAesKey", dd.get("tempAesKey")),
                         ("tempIv", dd.get("tempIv")),
                         ("hmacKey", sec.get("hmacKey"))):
        if not value:
            raise IdentityDeriveError(f"keySuite 响应缺少字段: {field}")

    hac = format_dk(dd["tempAesKey"], dd["tempIv"], sec["hmacKey"], priv)
    key_id = ks.get("keyId") or kid    # 实测响应必带 keyId（服务端按设备下发）
    if not ks.get("keyId"):
        logging.getLogger(__name__).warning("keySuite 响应无 keyId，退回 KID（可能被服务端拒签）")

    logging.getLogger(__name__).info(
        "签名身份派生成功: app_type=%s xdev=%s... key_id=%s... hac=%s...",
        app_type, str(xdev)[:12], str(key_id)[:12], hac.hex()[:8])
    return hac.hex(), key_id


__all__ = [
    "IdentityDeriveError",
    "LIVIS_DEVICE_KEY_ID",
    "build_data_to_sign",
    "build_key_suite_request",
    "format_dk",
    "derive_identity",
]
