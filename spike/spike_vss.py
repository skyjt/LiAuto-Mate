#!/usr/bin/env python3
"""验证 spike：纯 Python（无 App/模拟器）读取理想车辆实时状态。

链路（全部复用 ha-lixiang 的实测配方 + 本项目已验证的 token 刷新）：
  1. refresh_token(理想同学客户端) -> LISA access_token   [本项目 Nov-5 已验证]
  2. key_suite.derive_identity(livis, xdev, bearer) -> (hac_key, key_id)
     [ha-lixiang 配方：随机 hac 引导 + App 内置 RSA 私钥解 keySuite]
  3. /api/auth 换 vss:get-batch scope token              [本项目 Nov-5 已验证形式]
  4. 签名调 /ssp-cloud-vss-service/mobile/vss/get-batch   [ha-lixiang 配方]
仅只读，不做任何控制操作。
个人值经环境变量传入：LX_VIN / LX_REFRESH_TOKEN（可选 LX_MMKV_DIR，研究期设备态提取）。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# key_suite 用仓库内移植版（liam/auth/key_suite.py，源自 ha-lixiang，MIT；
# 见 docs/HA-LIXIANG-LICENSE），不再依赖外部克隆路径。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from liam.auth.key_suite import derive_identity  # noqa: E402

MMKV_DIR = os.environ.get("LX_MMKV_DIR", "")   # 可选：设备 MMKV 提取目录（研究期用法）
VIN = os.environ.get("LX_VIN", "")
LIVIS_CLIENT_ID = "40amUDKOdqQTaGDONZC1oY"
LIVIS_REDIRECT_URI = "https://account.lixiang.com/app-auth/livis"
AUD_VSS = "1j0vgTqagJUHuT6nLmbTGx"
SIGN_APP_VERSION = "8.25.4-10463"
EMPTY_MD5 = base64.b64encode(hashlib.md5(b"").digest()).decode()

VSS_PATHS = [
    "Vehicle.Body.DoorLockStatus.MainDoor",
    "Vehicle.Body.DoorLockStatus.TrunkDoor",
    "Vehicle.Body.DoorSwitchStatus.MainDoor",
    "Vehicle.Cabin.AC.SetTemp",
    "Vehicle.Cabin.AC.FrtACIncarTemp",
    "Vehicle.Cabin.AC.ExSpeedStatus",
    "Vehicle.Chassis.Tire.FLTireTemp",
    "Vehicle.Chassis.Tire.FRTireTemp",
    "Vehicle.Powertrain.Battery.ChargeStatus",
    "Vehicle.Powertrain.Battery.ResidueBattery",
]

UA_IPAD = f"m01/{SIGN_APP_VERSION} (iPad; iOS 16.7.12; Scale/2.00)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener():
    return urllib.request.build_opener(NoRedirect)


def mask(t, n=24):
    return (t[:n] + "...") if t and len(t) > n else (t or "")


def extract_refresh_tokens():
    """从设备拉取的 MMKV 里取 refresh token（BJ:/HZ:）；未设 LX_MMKV_DIR 则跳过。"""
    out = []
    if not MMKV_DIR:
        return out
    for f in ("my_sp_new", "my_sp"):          # 新的优先
        d = open(f"{MMKV_DIR}/{f}", "rb").read()
        for m in re.findall(rb"(?:BJ|HZ):[A-Za-z0-9_\-]{10,60}", d):
            out.append((f, m.decode()))
    return out


def refresh_access_token(refresh_token):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": LIVIS_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        "https://id.lixiang.com/api/token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "okhttp/4.12.0"})
    with opener().open(req, timeout=20) as r:
        return json.load(r)


def exchange_scope_token(bearer, scope, xdev):
    """POST /api/auth -> 302 Location 里的 access_token（Nov-5 实测形式）。"""
    body = urllib.parse.urlencode({
        "prompt": "none", "offline_access": "true",
        "redirect_uri": LIVIS_REDIRECT_URI, "scope": scope,
        "response_type": "token", "device_id": xdev,
        "audience": AUD_VSS, "client_id": LIVIS_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        "https://id.lixiang.com/api/auth", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Bearer {bearer}",
                 "User-Agent": "okhttp/4.12.0"})
    try:
        with opener().open(req, timeout=20) as r:
            j = json.loads(r.read().decode())
            if j.get("access_token"):
                return j["access_token"]
            return None
    except urllib.error.HTTPError as e:
        if e.code in (302, 303):
            loc = e.headers.get("Location", "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
            tok = q.get("access_token", [None])[0]
            if tok:
                return tok
            print(f"    [auth] 302 但 Location 无 token: {loc[:120]}")
            return None
        print(f"    [auth] HTTP {e.code}: {e.read().decode()[:160]}")
        return None


def signed_vss_get_batch(hac_hex, key_id, xdev, app_token, vss_token):
    body = json.dumps({"vin": VIN, "paths": VSS_PATHS})
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    md5b = base64.b64encode(hashlib.md5(body.encode()).digest()).decode()
    sts = "\n".join(["prod", SIGN_APP_VERSION, key_id, xdev, "POST", "*/*",
                     "zh-Hans-CN", md5b, "application/json", ts, nonce]) + "\n"
    sig = base64.b64encode(hmac.new(bytes.fromhex(hac_hex),
                                    sts.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "X-CHJ-Env": "prod", "X-CHJ-APP-Version": SIGN_APP_VERSION,
        "X-CHJ-Key": key_id, "X-CHJ-Deviceid": xdev,
        "X-CHJ-Timestamp": ts, "X-CHJ-Nonce": nonce, "X-CHJ-Sign": sig,
        "Content-MD5": md5b, "Content-Type": "application/json",
        "Content-Language": "zh-Hans-CN", "Accept": "*/*",
        "X-CHJ-Version": SIGN_APP_VERSION, "X-CHJ-DeviceType": "2",
        "X-CHJ-ModelName": "IOS", "X-CHJ-Tag": "1",
        "X-CHJ-TOKEN": app_token, "X-CHJ-VIN": VIN,
        "Authorization": f"Bearer {vss_token}",
        "User-Agent": UA_IPAD,
    }
    req = urllib.request.Request(
        "https://api-app.lixiang.com/ssp-cloud-vss-service/mobile/vss/get-batch",
        data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:400]}


def main():
    if not VIN:
        print("!! 缺少 LX_VIN（车辆识别码）")
        return 1
    # ---- 1. 刷新 token（优先 LX_REFRESH_TOKEN，其次可选 MMKV 目录）----
    bearer = None
    used = None
    rt_env = os.environ.get("LX_REFRESH_TOKEN", "")
    candidates = ([("env", rt_env)] if rt_env else []) + extract_refresh_tokens()
    for src, rt in candidates:
        print(f"[1] 尝试刷新 refresh_token（来源 {src}, {rt[:6]}...）")
        try:
            tok = refresh_access_token(rt)
            if tok.get("access_token"):
                bearer = tok["access_token"]
                used = (src, rt)
                print(f"    OK access_token={mask(bearer)} "
                      f"expires_in={tok.get('expires_in')}")
                break
            print(f"    响应无 token: {json.dumps(tok)[:160]}")
        except Exception as e:
            print(f"    失败: {e}")
    if not bearer:
        print("!! 所有 refresh_token 均失效，需要重新登录（短信/密码）。")
        return 1

    # ---- 2. keySuite 派生签名身份 ----
    xdev = uuid.uuid4().hex          # 本设备新身份（ha-lixiang「每设备运行时身份」做法）
    print(f"[2] keySuite 派生身份 xdev={xdev}")
    try:
        hac_hex, key_id = derive_identity("livis", xdev, bearer)
        print(f"    OK key_id={key_id} hac={hac_hex[:8]}... (32B)")
    except Exception as e:
        print(f"    失败: {e}")
        return 1

    # ---- 3. 换 VSS scope token ----
    for scope in ("vss:get-batch",
                  "remote-wakeup:wakeup veh-ctrl:cmd-result-get "
                  "veh-ctrl:cmd-send vss:get-batch task-master"):
        print(f"[3] 换 scope token: {scope[:40]}...")
        vss_tok = exchange_scope_token(bearer, scope, xdev)
        if vss_tok:
            print(f"    OK vss_token={mask(vss_tok, 18)}")
            break
        print("    失败，尝试下一个 scope 组合")
    else:
        print("!! scope token 换取失败")
        return 1

    # ---- 4. vss/get-batch ----
    print(f"[4] 调 vss/get-batch（{len(VSS_PATHS)} 条路径，VIN={VIN}）")
    resp = signed_vss_get_batch(hac_hex, key_id, xdev, bearer, vss_tok)
    print(f"    code={resp.get('code')} msg={resp.get('msg') or resp.get('message')}")
    if resp.get("http_error"):
        print(f"    HTTP {resp['http_error']} body={resp.get('body')}")
        return 1
    items = resp.get("items") or (resp.get("data") or {}).get("items") or []
    if not items:
        print(f"    !! 无 items，完整响应: {json.dumps(resp, ensure_ascii=False)[:500]}")
        return 1
    print("    ==== 车辆实时状态 ====")
    for it in items:
        dp = it.get("dp") or {}
        print(f"    {it.get('path'):60s} = {dp.get('value')!r:12} @ {str(dp.get('tsFormat'))[:19]}")
    print("\n[✓] SPIKE 成功：纯 Python 全链路（无 App/模拟器）读到了车辆实时数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
