#!/usr/bin/env python3
"""最终 spike：用 browser_login 拿到的新会话完成 车辆状态读取全链路。
换 VSS scope token → keySuite 派生 hac_key → 签名调 vss/get-batch。
"""
import json
import os
import sys
import urllib.parse

import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
from spike_vss import signed_vss_get_batch, derive_identity  # noqa: E402（key_suite 走 liam.auth）

SESS = os.path.join(_DIR, "session.json")
CLIENT_ID = "40amUDKOdqQTaGDONZC1oY"
SUBIDaaS_REDIRECT = "https://app.lixiang.com/login/subidaas/callback"
AUD_VSS = "1j0vgTqagJUHuT6nLmbTGx"


def main():
    sess = json.load(open(SESS))
    bearer, dev = sess["bearer"], sess["device_id"]
    print(f"[0] 会话 bearer={bearer[:20]}... dev={dev}")
    print(f"    cookies: {sorted(sess['cookies'].keys())}")

    s = requests.Session()
    for name, val in sess["cookies"].items():
        for dom in ("id.lixiang.com", "account.lixiang.com"):
            s.cookies.set(name, val, domain=dom)

    # 1. 换 VSS scope token（带 sso_token 会话）
    vss_tok = None
    for scope in ("vss:get-batch",
                  "remote-wakeup:wakeup veh-ctrl:cmd-result-get "
                  "veh-ctrl:cmd-send vss:get-batch task-master"):
        r = s.post("https://id.lixiang.com/api/auth", data={
            "client_id": CLIENT_ID, "audience": AUD_VSS, "response_type": "token",
            "scope": scope, "redirect_uri": SUBIDaaS_REDIRECT, "offline_access": "true",
            "prompt": "none", "device_id": dev,
        }, timeout=20, allow_redirects=False,
            headers={"User-Agent": "okhttp/4.12.0",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": f"Bearer {bearer}"})
        loc = r.headers.get("Location", "")
        if not loc and r.status_code == 200:
            loc = (r.json() or {}).get("location", "")
        frag = urllib.parse.urlparse(loc).fragment
        fq = urllib.parse.parse_qs(frag)
        vss_tok = (fq.get("access_token") or [""])[0]
        print(f"[1] scope「{scope[:24]}…」HTTP {r.status_code} token={vss_tok[:18]}… "
              f"expires={fq.get('expires_in', ['?'])[0]}")
        if vss_tok:
            break
    if not vss_tok:
        print("!! scope token 换取失败"); return 1

    # 2. keySuite 派生签名身份（用登录设备号，保持一致）
    print(f"[2] keySuite 派生身份…")
    try:
        hac_hex, key_id = derive_identity("livis", dev, bearer)
        print(f"    OK key_id={key_id} hac={hac_hex[:8]}…")
    except Exception as e:
        print(f"    失败: {e}"); return 1

    # 3. vss/get-batch
    print("[3] 调 vss/get-batch …")
    resp = signed_vss_get_batch(hac_hex, key_id, dev, bearer, vss_tok)
    print(f"    code={resp.get('code')} msg={resp.get('msg') or resp.get('message')}")
    if resp.get("http_error"):
        print(f"    HTTP {resp['http_error']} body={resp.get('body')}")
        return 1
    items = resp.get("items") or (resp.get("data") or {}).get("items") or []
    if not items:
        print(f"    !! 无 items: {json.dumps(resp, ensure_ascii=False)[:600]}")
        return 1
    print("    ==== 车辆实时状态（纯 Python，无 App/模拟器）====")
    for it in items:
        dp = it.get("dp") or {}
        print(f"    {it.get('path'):58s} = {dp.get('value')!r:10} @ {str(dp.get('tsFormat'))[:19]}")
    print("\n[✓✓] 全链路打通。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
