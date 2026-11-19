"""X-CHJ-Sign 签名（11 段 HMAC string-to-sign，2026-11-19 本项目实测配方）。

与 Android 2.9.0 的区别（实测结论，勿改）：
  · 第 6 段 = Accept（`*/*`），而非空串
  · Content-Language = zh-Hans-CN 进第 7 段
  · X-CHJ-TOKEN = 当前登录主 Bearer（LISA）
移植自 ha-lixiang signer.py（MIT）+ 本项目 spike_vss.py 实测参数。

纯函数，无 IO —— 单测直接对拍固定向量。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

from .constants import SIGN_APP_VERSION


def content_md5(body: str) -> str:
    """body 的 Content-MD5（base64）。空 body 用空串的 MD5。"""
    return base64.b64encode(hashlib.md5(body.encode()).digest()).decode()


def sign_headers(
    hac_hex: str,
    key_id: str,
    device_id: str,
    bearer: str,
    *,
    method: str = "POST",
    accept: str = "*/*",
    content_type: str = "application/json",
    content_language: str = "zh-Hans-CN",
    body: str = "",
    vin: str = "",
    app_version: str = SIGN_APP_VERSION,
) -> dict[str, str]:
    """组装一次调用的完整签名头。

    sts = "prod\\n" + ver + key + deviceId + METHOD + Accept + lang +
           contentMD5 + contentType + ts + nonce，末尾补 "\\n"，
           HMAC-SHA256(key=hac 原始 32B) → base64。
    """
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    md5b = content_md5(body)
    sts = "\n".join([
        "prod", app_version, key_id, device_id, method, accept,
        content_language, md5b, content_type, ts, nonce,
    ]) + "\n"
    sig = base64.b64encode(
        hmac.new(bytes.fromhex(hac_hex), sts.encode(), hashlib.sha256).digest()
    ).decode()
    headers: dict[str, str] = {
        "X-CHJ-Env": "prod",
        "X-CHJ-APP-Version": app_version,
        "X-CHJ-Version": app_version,
        "X-CHJ-Key": key_id,
        "X-CHJ-Deviceid": device_id,
        "X-CHJ-Timestamp": ts,
        "X-CHJ-Nonce": nonce,
        "X-CHJ-Sign": sig,
        "Content-MD5": md5b,
        "Content-Type": content_type,
        "Content-Language": content_language,
        "Accept": accept,
        "X-CHJ-DeviceType": "2",
        "X-CHJ-ModelName": "IOS",
        "X-CHJ-Tag": "1",
        "X-CHJ-TOKEN": bearer,
        "User-Agent": f"m01/{app_version} (iPad; iOS 16.7.12; Scale/2.00)",
    }
    if vin:
        headers["X-CHJ-VIN"] = vin
    return headers
