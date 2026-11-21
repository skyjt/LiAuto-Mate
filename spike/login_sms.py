#!/usr/bin/env python3
"""纯 Python 短信登录理想账号（无浏览器/无 App）。

流程还原自 2026-11-05 的成功登录抓包（mitmproxy 复现）：
  POST /api/devices → PKCE POST /api/auth(auth_session) → POST /api/reset
  → POST /api/challenge(要求 SLIDING?) → [滑块] → sliding_proof(发短信)
  → verify_code → challenge_token → POST /api/login(SMS_CODE) → POST /api/login(LI_USER)
  → code → POST /api/token(authorization_code) → sso_token → scope 交换

用法（手机号经环境变量 LX_PHONE 传入，不落代码）：
  LX_PHONE=<手机号> login_sms.py send     # 发起登录、发短信（若无滑块），状态存 login_state.pkl
  LX_PHONE=<手机号> login_sms.py verify <6位验证码>   # 完成登录 → 派生身份 → 换 VSS token → vss/get-batch
"""
import base64
import hashlib
import json
import os
import pickle
import secrets
import sys
import time
import urllib.parse

import requests

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_state.pkl")
PHONE = os.environ.get("LX_PHONE", "")
CLIENT_ID = "40amUDKOdqQTaGDONZC1oY"
LOGIN_AUD = "5iIapSfVJlln0vU0OzUCH9"      # PKCE /api/auth 的 audience
MFA_AUD = "1GDY9heeywMdHEEwDneHfx"        # mfa_verify challenge 的 audience
REDIRECT = "https://account.lixiang.com/app-auth/livis"
SUBIDaaS_REDIRECT = "https://app.lixiang.com/login/subidaas/callback"
AUD_VSS = "1j0vgTqagJUHuT6nLmbTGx"
UA = ("Mozilla/5.0 (Linux; Android 14; sdk_gphone64_arm64 Build/UE1A.230829.050; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 "
      "Mobile Safari/537.36 Livis/2.9.0")
BASE = "https://id.lixiang.com"
BASE_ACCT = "https://account.lixiang.com"


def new_session(dev):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Origin": BASE_ACCT,
                      "Referer": BASE_ACCT + "/", "Accept": "application/json, text/plain, */*"})
    s.verify = True
    for dom in ("account.lixiang.com", "id.lixiang.com"):
        s.cookies.set("X-LX-Deviceid", dev, domain=dom)
        s.cookies.set("authli_device_id", dev, domain=dom)
    s.cookies.set("isapp", "1", domain="account.lixiang.com")
    return s


def phase_send():
    if not PHONE:
        print("!! 缺少 LX_PHONE（登录手机号），用法：LX_PHONE=<手机号> login_sms.py send")
        return 1
    dev = secrets.token_hex(16)
    s = new_session(dev)

    # 1. 设备注册
    r = s.post(BASE + "/api/devices", json={
        "client_id": CLIENT_ID, "device_id": dev, "user_agent": UA,
        "model": "sdk_gphone64_arm64", "manufacturer": "", "os": "Android",
        "os_version": "14", "screen_height": 732, "screen_width": 412, "app_version": "",
    }, timeout=20)
    print(f"[devices] {r.status_code} {r.text[:80]}")

    # 2. PKCE /api/auth
    cv = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    cc = base64.urlsafe_b64encode(hashlib.sha256(cv.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(21)[:21]
    r = s.post(BASE + "/api/auth", data={
        "client_id": CLIENT_ID, "device_id": dev, "response_type": "code",
        "redirect_uri": REDIRECT, "offline_access": "true", "state": state,
        "audience": LOGIN_AUD, "scope": "iam:client:type:lisa openid",
        "prompt": "login", "required_scope": "iam:client:type:lisa",
        "code_challenge": cc, "code_challenge_method": "S256",
    }, timeout=20, allow_redirects=False)
    print(f"[auth] {r.status_code} auth_session={'auth_session' in s.cookies}")
    if r.status_code not in (200, 300, 302):
        print(r.text[:200]); return 1

    # 3. reset
    r = s.post(BASE + "/api/reset", timeout=20)
    print(f"[reset] {r.status_code}")

    # 4. 发短信（关键：是否要求滑块）
    r = s.post(BASE + "/api/challenge", data={
        "channel_type": "mobile", "channel": "+86" + PHONE,
        "audience": MFA_AUD, "client_id": CLIENT_ID,
        "type": "li_user:livis:mfa_verify",
    }, timeout=20)
    print(f"[challenge] {r.status_code} {r.text[:200]}")
    try:
        j = r.json()
    except Exception:
        print("!! challenge 响应非 JSON"); return 1
    req = (j.get("requires") or {})
    if "SLIDING" in req:
        print("!! 服务端要求顶象滑块，纯 Python 路径被风控拦截（需浏览器方案）")
        return 2
    if not j.get("cid"):
        print("!! 无 cid"); return 1
    print(f"    cid={j['cid']} → 短信应已发送到 +86{PHONE[-4:]}…")

    pickle.dump({"sess": s, "cid": j["cid"], "cv": cv, "dev": dev},
                open(STATE, "wb"))
    print(f"状态已存 {STATE}，等待验证码后运行: verify <code>")
    return 0


def phase_verify(code):
    st = pickle.load(open(STATE, "rb"))
    s, cid, cv, dev = st["sess"], st["cid"], st["cv"], st["dev"]
    ctype = "li_user:livis:mfa_verify"

    # 5. verify_code → challenge_token
    r = s.post(f"{BASE}/api/challenge/{ctype}/{cid}",
               data={"verify_code": code}, timeout=20)
    print(f"[verify_code] {r.status_code} {r.text[:120]}")
    ct = r.json().get("challenge_token")
    if not ct:
        print("!! 验证码校验失败"); return 1

    # 6. /api/login (SMS_CODE proof)
    r = s.post(BASE + "/api/login", json={
        "client_id": CLIENT_ID, "proof": ct, "connection": "SMS_CODE"},
        headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20, allow_redirects=False)
    print(f"[login#1] {r.status_code} {r.text[:100]}")

    # 7. /api/login (LI_USER) → code
    r = s.post(BASE + "/api/login", json={
        "client_id": CLIENT_ID, "connection": "LI_USER", "user_tip": "+86" + PHONE},
        headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20, allow_redirects=False)
    print(f"[login#2] {r.status_code}")
    loc = r.headers.get("Location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    authz_code = (q.get("code") or [""])[0]
    print(f"    code={authz_code[:14]}... sso_token={'sso_token' in s.cookies}")
    if not authz_code:
        print(f"!! 无 code, location={loc[:150]}"); return 1

    # 8. /api/token
    r = s.post(BASE + "/api/token", data={
        "client_id": CLIENT_ID, "grant_type": "authorization_code",
        "code": authz_code, "code_verifier": cv}, timeout=20)
    tok = r.json()
    print(f"[token] {r.status_code} keys={list(tok.keys())}")
    bearer = tok.get("access_token") or tok.get("id_token")
    refresh = tok.get("refresh_token")
    if not bearer:
        print("!! 无 access_token"); return 1
    print(f"    bearer={bearer[:24]}... refresh={str(refresh)[:12]}...")

    out = {"bearer": bearer, "refresh_token": refresh, "dev": dev,
           "cookies": {c.name: c.value for c in s.cookies}}
    json.dump(out, open(STATE.replace(".pkl", ".json"), "w"), ensure_ascii=False, indent=1)
    print(f"会话已存 {STATE.replace('.pkl', '.json')}")

    # 9. 用 sso_token 换 VSS scope token（两种 scope）
    for label, scope in (("vss单scope", "vss:get-batch"),
                         ("5项mesh scope", "remote-wakeup:wakeup veh-ctrl:cmd-result-get "
                                            "veh-ctrl:cmd-send vss:get-batch task-master")):
        r = s.post(BASE + "/api/auth", data={
            "client_id": CLIENT_ID, "audience": AUD_VSS, "response_type": "token",
            "scope": scope, "redirect_uri": SUBIDaaS_REDIRECT, "offline_access": "true",
            "prompt": "none", "device_id": dev}, timeout=20, allow_redirects=False)
        loc = r.headers.get("Location", "") or r.json().get("location", "")
        frag = urllib.parse.urlparse(loc).fragment
        fq = urllib.parse.parse_qs(frag)
        at = (fq.get("access_token") or [""])[0]
        print(f"[scope:{label}] {r.status_code} token={at[:16]}... "
              f"expires={fq.get('expires_in', ['?'])[0]}")
        if at:
            out[f"vss_token_{label}"] = at
    json.dump(out, open(STATE.replace(".pkl", ".json"), "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "send":
        sys.exit(phase_send())
    elif cmd == "verify" and len(sys.argv) > 2:
        sys.exit(phase_verify(sys.argv[2]))
    else:
        print(__doc__)
        sys.exit(1)
