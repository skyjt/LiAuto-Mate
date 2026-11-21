#!/usr/bin/env python3
"""诊断 /api/auth login_required：不同 device_id / audience / scope / client 组合。"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

LIVIS_CLIENT_ID = "40amUDKOdqQTaGDONZC1oY"
MAIN_CLIENT_ID = "2AQClOaegaA7XecMSFx1p"
LIVIS_REDIRECT = "https://account.lixiang.com/app-auth/livis"
MAIN_REDIRECT = "https://app.lixiang.com/login/subidaas/callback"

# 原实测用抓包 deviceId（已脱敏）；复现诊断时经环境变量传入自己的设备 ID
DEV_NEW = os.environ.get("LX_DEVICE_ID", "<your-device-id>")
DEV_OLD = os.environ.get("LX_DEVICE_ID_OLD", "<old-device-id>")

COMBOS = [
    # (label, audience, scope, device_id, client_id, redirect_uri)
    ("VSS + 最后会话设备",   "1j0vgTqagJUHuT6nLmbTGx", "vss:get-batch", DEV_NEW, LIVIS_CLIENT_ID, LIVIS_REDIRECT),
    ("VSS + 旧设备",         "1j0vgTqagJUHuT6nLmbTGx", "vss:get-batch", DEV_OLD, LIVIS_CLIENT_ID, LIVIS_REDIRECT),
    ("service-card + 新设备", "26FehzsHlrllCSaqI9bFYG", "login", DEV_NEW, LIVIS_CLIENT_ID, LIVIS_REDIRECT),
    ("子IDaaS + 新设备",      "6Z7QEqTF55bh07MFEoJLuP", "login", DEV_NEW, LIVIS_CLIENT_ID, LIVIS_REDIRECT),
    ("VAT + 新设备",          "3WNA2URKRZ6W11qQKo5YJj", f"create_port_stream:{os.environ.get('LX_VIN', '<VIN>')}", DEV_NEW, LIVIS_CLIENT_ID, LIVIS_REDIRECT),
    ("VSS + 主App客户端",     "1j0vgTqagJUHuT6nLmbTGx", "vss:get-batch", DEV_NEW, MAIN_CLIENT_ID, MAIN_REDIRECT),
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def refresh():
    rt = os.environ.get("LX_REFRESH_TOKEN", "")
    if not rt:
        print("!! 缺少 LX_REFRESH_TOKEN（理想同学客户端 refresh_token）")
        sys.exit(1)
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt,
                                   "client_id": LIVIS_CLIENT_ID}).encode()
    req = urllib.request.Request("https://id.lixiang.com/api/token", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": "okhttp/4.12.0"})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
        return json.load(r)["access_token"]


def try_auth(bearer, audience, scope, device_id, client_id, redirect_uri):
    body = urllib.parse.urlencode({
        "prompt": "none", "offline_access": "true", "redirect_uri": redirect_uri,
        "scope": scope, "response_type": "token", "device_id": device_id,
        "audience": audience, "client_id": client_id,
    }).encode()
    req = urllib.request.Request("https://id.lixiang.com/api/auth", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Authorization": f"Bearer {bearer}",
                                          "User-Agent": "okhttp/4.12.0"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
            j = json.loads(r.read().decode())
            return ("200-JSON", bool(j.get("access_token")), j.get("error", ""))
    except urllib.error.HTTPError as e:
        if e.code in (302, 303):
            loc = e.headers.get("Location", "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
            ok = bool(q.get("access_token"))
            err = (q.get("error") or [""])[0]
            return ("302", ok, err if not ok else "access_token ✓")
        return (f"HTTP{e.code}", False, e.read().decode()[:100])


def main():
    bearer = refresh()
    print(f"bearer = {bearer[:20]}...\n")
    for label, aud, scope, dev, cid, redir in COMBOS:
        st, ok, detail = try_auth(bearer, aud, scope, dev, cid, redir)
        mark = "✓" if ok else "✗"
        print(f"{mark} [{label}] {st} {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
