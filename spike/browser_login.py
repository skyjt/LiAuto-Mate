#!/usr/bin/env python3
"""浏览器短信登录（Playwright + 真实 Chrome，headed）。

- 自动填手机号 → 触发滑块 → 自动缺口检测+轨迹拖拽（移植自 emulator 时代 solve_captcha.py）
- 自动滑块失败则提示人工拖拽（ headed 窗口）
- 滑块通过后等待 sms_code.txt（由外部写入验证码）
- 完成登录后从网络层抓 /api/token（主 Bearer/refresh）+ 导出 cookies → session.json

运行：LX_PHONE=<手机号> ./.venv/bin/python browser_login.py  （前台跑，日志逐行输出）
"""
import base64
import io
import json
import os
import re
import sys
import time
from collections import deque

import numpy as np
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

_DIR = os.path.dirname(os.path.abspath(__file__))
PHONE = os.environ.get("LX_PHONE", "")
CODE_FILE = os.path.join(_DIR, "sms_code.txt")
OUT = os.path.join(_DIR, "session.json")
SHOT = os.path.join(_DIR, "shot_%d.png")
UA = ("Mozilla/5.0 (Linux; Android 14; sdk_gphone64_arm64 Build/UE1A.230829.050; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile "
      "Safari/537.36 Livis/2.9.0")

net = {"device_id": None, "token": None}


def log(*a):
    print(*a, flush=True)


def shot(page, i):
    page.screenshot(path=SHOT % i)
    log(f"SCREENSHOT: {SHOT % i}")


# ---------------- 网络监听 ----------------
def attach_net(page):
    def on_req(req):
        try:
            if "id.lixiang.com" not in req.url: return
            if req.method != "POST": return
            b = req.post_data or ""
            if "device_id=" in b:
                m = re.search(r"device_id=([0-9a-f]{32})", b)
                if m and not net["device_id"]: net["device_id"] = m.group(1)
            if "/api/devices" in req.url and req.post_data:
                try:
                    j = json.loads(req.post_data)
                    if j.get("device_id"): net["device_id"] = j["device_id"]
                except Exception: pass
        except Exception: pass

    def on_resp(resp):
        try:
            if resp.url.endswith("/api/token") and resp.status == 200:
                net["token"] = resp.json()
                log(f"[net] /api/token 抓到: keys={list((net['token'] or {}).keys())}")
        except Exception: pass

    page.on("request", on_req)
    page.on("response", on_resp)


# ---------------- 滑块求解（移植 solve_captcha.py） ----------------
GET_IMGS = r"""(() => {
  const cv=document.querySelector('.dx_captcha_basic_pic canvas') || document.querySelector('canvas');
  const piece=document.querySelector('.dx_captcha_basic_sub-slider img');
  const pic=document.querySelector('.dx_captcha_basic_pic');
  const sub=[...document.querySelectorAll('.dx_captcha_basic_sub-slider')].find(e=>e.getBoundingClientRect().width>0);
  const sl=[...document.querySelectorAll('.dx_captcha_basic_slider')].find(e=>e.getBoundingClientRect().width>0);
  const r=e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height}};
  return JSON.stringify({canvas:cv?cv.toDataURL('image/png'):null, cvr:cv?r(cv):null,
    piece:piece?piece.src:null, sub:sub?r(sub):null, slider:sl?r(sl):null,
    ok:(()=>{const e=[...document.querySelectorAll('.dx_captcha_basic_bar-success')].find(x=>x.getBoundingClientRect().width>0);return e?getComputedStyle(e).display!=='none':false;})(),
    fail:(()=>{const e=[...document.querySelectorAll('.dx_captcha_basic_bar-fail')].find(x=>x.getBoundingClientRect().width>0);return e?getComputedStyle(e).display!=='none':false;})()});
})()"""


def find_hole(bg):
    im = Image.fromarray(bg.astype(np.uint8))
    m = np.asarray(im.filter(ImageFilter.BoxBlur(3))).astype(float)
    s = np.asarray(Image.fromarray((bg ** 2).clip(0, 255).astype(np.uint8))
                   .filter(ImageFilter.BoxBlur(3))).astype(float)
    std = np.sqrt(np.maximum(s - m ** 2, 0))
    mask = (bg < 75) & (std < 20)
    H, W = mask.shape
    seen = np.zeros_like(mask, bool)
    comps = []
    for y in range(H):
        for x in range(W):
            if mask[y, x] and not seen[y, x]:
                q = deque([(y, x)]); seen[y, x] = True; pts = []
                while q:
                    cy, cx = q.popleft(); pts.append((cy, cx))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                                seen[ny, nx] = True; q.append((ny, nx))
                if 400 < len(pts) < H * W * 0.5:
                    ys = [p[0] for p in pts]; xs = [p[1] for p in pts]
                    lum = bg[[p[0] for p in pts], [p[1] for p in pts]].mean()
                    comps.append(dict(size=len(pts), x0=min(xs), x1=max(xs),
                                      y0=min(ys), y1=max(ys),
                                      cx=(min(xs) + max(xs)) / 2,
                                      cy=(min(ys) + max(ys)) / 2, mean=float(lum)))
    comps.sort(key=lambda c: -c["size"])
    return [c for c in comps
            if 800 < c["size"] < 6000 and (c["x1"] - c["x0"]) > 35 and (c["y1"] - c["y0"]) > 35]


CAPTCHA_STATE = r"""(() => {
  const vis = e => { try { return e && e.getBoundingClientRect().width > 0 &&
    getComputedStyle(e).display !== 'none'; } catch(x) { return false; } };
  const wrap = [...document.querySelectorAll('.dx_captcha_basic_wrapper')].find(vis);
  const ok = [...document.querySelectorAll('.dx_captcha_basic_bar-success')].find(vis);
  const fail = [...document.querySelectorAll('.dx_captcha_basic_bar-fail')].find(vis);
  const body = document.body.innerText || '';
  const codeInput = [...document.querySelectorAll('input')].some(
    i => vis(i) && ((i.placeholder || '').includes('验证码') || (i.getAttribute('aria-label') || '').includes('验证码')));
  return JSON.stringify({
    widget: !!wrap, ok: !!ok, fail: !!fail,
    detecting: /智能检测/.test(body),
    countdown: /重新发送|后重发|重新获取/.test(body),
    codeInput, text: body.slice(0, 120)
  });
})()"""


def captcha_result(page):
    """统一判定：成功 = ok 条 或 组件消失且出现倒计时/验证码输入。「智能检测中」视为进行中。"""
    d = json.loads(page.evaluate(CAPTCHA_STATE))
    if d["ok"]:
        return "solved", d
    if d["detecting"]:
        return "detecting", d
    if not d["widget"] and (d["countdown"] or d["codeInput"]):
        return "solved", d
    if d["fail"]:
        return "failed", d
    if not d["widget"]:
        return "gone", d
    return "present", d


def solve_once(page, cdp):
    d = json.loads(page.evaluate(GET_IMGS))
    if d.get("canvas") is None:
        r, st = captcha_result(page)
        if r == "solved":
            return True, "already_solved"
        return False, "no_captcha"
    if d.get("ok"): return True, "already_ok"
    sub, sl, cvr = d.get("sub"), d.get("slider"), d.get("cvr")
    if not sub or not sl or not cvr:
        return False, "layout_missing"
    bg = np.asarray(Image.open(io.BytesIO(base64.b64decode(
        d["canvas"].split(",", 1)[1]))).convert("L")).astype(float)
    u = d.get("piece")
    if not u:
        return False, "no_piece"
    if u.startswith("data:"):
        raw = base64.b64decode(u.split(",", 1)[1])
    else:
        # 外链拼图块（static4.dingxiang-inc.com）→ Python 侧下载
        import urllib.request
        rq = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(rq, timeout=10).read()
        log(f"    [solver] 拼图块已下载 {len(raw)}B")
    H, W = bg.shape
    cvr, sub, sl = d["cvr"], d["sub"], d["slider"]
    scale = cvr["w"] / W
    hole = find_hole(bg)
    comps = [c for c in hole if 30 < (c["x1"] - c["x0"]) < 120]
    if not comps:
        return False, "no_hole"
    # 与 piece 纵向位置对齐的候选
    piece_y_cv = sub["y"] + sub["h"] / 2
    exp_y = (piece_y_cv - cvr["y"]) / (cvr["h"] / H)
    cand = [c for c in comps if abs(c["cy"] - exp_y) < 50] or comps
    h = min(cand, key=lambda z: z["mean"])
    hole_cx_css = cvr["x"] + h["cx"] * scale
    piece_cx_css = sub["x"] + sub["w"] / 2
    dx = hole_cx_css - piece_cx_css
    hx = sl["x"] + sl["w"] / 2
    hy = sl["y"] + sl["h"] / 2
    log(f"    [solver] hole_cx={hole_cx_css:.0f} piece_cx={piece_cx_css:.0f} dx={dx:.0f}")
    # 轨迹拖拽（ease-out + 抖动）
    import random
    random.seed(int(time.time()))
    steps, dur = 30, 0.7
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart",
             "touchPoints": [{"x": hx, "y": hy, "id": 1}]})
    for i in range(1, steps + 1):
        t = i / steps
        x = hx + dx * (1 - (1 - t) ** 2.2) + random.uniform(-0.4, 0.4)
        y = hy + random.uniform(-0.8, 0.8)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                 "touchPoints": [{"x": x, "y": y, "id": 1}]})
        time.sleep(dur / steps)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    time.sleep(2.5)
    r, st = captcha_result(page)
    log(f"    [solver] 拖拽后状态: {r} text={st['text'][:60]!r}")
    if r == "solved":
        return True, "solved"
    if r == "failed":
        try:
            page.evaluate("""(() => {
              const b=[...document.querySelectorAll('.dx_captcha_basic_refresh, .dx_captcha_basic-btn-refresh')]
                .find(e=>e.getBoundingClientRect().width>0); if(b){b.click();return true;} return false;})()""")
            time.sleep(1.5)
        except Exception: pass
        return False, "failed_retry"
    # present/gone 但未确认 → 稍候再判一次
    time.sleep(2.0)
    r2, _ = captcha_result(page)
    if r2 == "solved":
        return True, "solved_late"
    return False, f"state_{r}"


def wait_captcha_and_solve(page, cdp, max_auto=6):
    # 等滑块组件/隐形检测完成（或页面已自行进入下一步）
    for _ in range(120):      # 最多 60s，覆盖「智能检测中」
        r, st = captcha_result(page)
        if r == "solved":
            return "already_solved"
        if r in ("present", "failed"):
            d = json.loads(page.evaluate(GET_IMGS))
            if d.get("canvas"):
                break
        time.sleep(0.5)
    else:
        return "no_captcha"
    for i in range(max_auto):
        ok, why = solve_once(page, cdp)
        log(f"    [solver#{i+1}] {ok} {why}")
        if ok: return "solved"
        if why == "no_captcha": return "no_captcha"
        if why == "already_solved": return "solved"
        # 重试前等组件重新就绪
        for _ in range(12):
            d = json.loads(page.evaluate(GET_IMGS))
            if d.get("canvas") and d.get("sub") and d.get("slider"):
                break
            r, st = captcha_result(page)
            if r == "solved": return "solved_late"
            time.sleep(1)
    log("MANUAL_ACTION_NEEDED: 请在浏览器窗口里手动拖动滑块完成验证")
    for _ in range(180):
        r, st = captcha_result(page)
        if r == "solved": return "manual_solved"
        time.sleep(1)
    return "timeout"


def read_sms_code(not_before):
    """从 Mac chat.db 读理想同学验证码；只认 not_before 之后的新短信。"""
    import sqlite3
    import datetime as dt
    try:
        db = sqlite3.connect(
            "file:" + os.path.expanduser("~/Library/Messages/chat.db") + "?mode=ro", uri=True)
        rows = db.execute("""
            SELECT text, datetime(date/1000000000 + 978307200, 'unixepoch', 'localtime')
            FROM message
            WHERE text IS NOT NULL AND text LIKE '%验证码%' AND text LIKE '%理想%'
            ORDER BY date DESC LIMIT 5""").fetchall()
        for t, ts in rows:
            m = re.search(r"(\d{6})", t or "")
            if not m: continue
            try:
                age = dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception: continue
            if age >= not_before:
                return m.group(1)
        return None
    except Exception as e:
        log(f"    [sms] chat.db 不可读（{e}），等手动写码文件")
        return None


# ---------------- 主流程 ----------------
def main():
    if not PHONE:
        log("!! 缺少 LX_PHONE（登录手机号），用法：LX_PHONE=<手机号> browser_login.py")
        return 1
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=False,
                                     args=["--window-size=480,900"])
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 412, "height": 732},
            is_mobile=True, has_touch=True, locale="zh-CN")
        page = ctx.new_page()
        attach_net(page)
        cdp = ctx.new_cdp_session(page)

        dev = __import__("secrets").token_hex(16)
        entry = ("https://account.lixiang.com/app-auth/livis?client_id=40amUDKOdqQTaGDONZC1oY"
                 "&audience=5iIapSfVJlln0vU0OzUCH9&scope=iam%3Aclient%3Atype%3Alisa"
                 f"&device_id={dev}&prompt=login&required_scope=iam%3Aclient%3Atype%3Alisa")
        ctx.add_cookies([
            {"name": "X-LX-Deviceid", "value": dev, "domain": "account.lixiang.com", "path": "/"},
            {"name": "X-LX-Deviceid", "value": dev, "domain": "id.lixiang.com", "path": "/"},
            {"name": "authli_device_id", "value": dev, "domain": "account.lixiang.com", "path": "/"},
            {"name": "authli_device_id", "value": dev, "domain": "id.lixiang.com", "path": "/"},
            {"name": "isapp", "value": "1", "domain": "account.lixiang.com", "path": "/"},
        ])
        log("[1] 打开登录页（app-auth 入口）… dev=" + dev)
        page.goto(entry, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        # 若停在 identifier 之外的怪状态，直接补导航
        if "/identifier" not in page.url:
            page.goto("https://account.lixiang.com/login/app/livis/identifier",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
        shot(page, 1)
        log("URL:", page.url)
        log("BODY:", page.evaluate("document.body.innerText.slice(0, 200)"))

        # 勾选协议（若有）
        try:
            page.evaluate("""(() => {
              for (const c of document.querySelectorAll('input[type=checkbox]')) {
                if (c.offsetParent && !c.checked) c.click();
              }})()""")
        except Exception: pass

        # 填手机号
        filled = False
        for sel in ("input[type=tel]", "input[placeholder*='手机']",
                    "input[placeholder*='电话']", "input:not([type=password])"):
            try:
                el = page.locator(sel).first
                if el.is_visible():
                    el.fill(PHONE)
                    filled = True
                    log(f"[2] 手机号已填入 {sel}")
                    break
            except Exception: continue
        if not filled:
            log("!! 未找到手机号输入框，截图供诊断"); shot(page, 2); return 1
        page.wait_for_timeout(600)

        # 勾选「我已阅读并同意」：取文本坐标，鼠标点其左侧的圆圈
        rect = page.evaluate(r"""(() => {
          for (const n of document.querySelectorAll('*')) {
            if (n.childElementCount) continue;
            const t = (n.textContent || '').trim();
            if (t.includes('我已阅读并同意')) {
              const r = n.getBoundingClientRect();
              return {x: r.x, y: r.y, w: r.width, h: r.height};
            }
          }
          return null;
        })()""")
        if rect:
            page.mouse.click(rect["x"] - 22, rect["y"] + rect["h"] / 2)
            log(f"[3] 点击协议圆圈 @({rect['x'] - 22:.0f},{rect['y'] + rect['h'] / 2:.0f})")
        else:
            log("[3] 未定位到协议文本（可能已勾选或结构不同）")
        page.wait_for_timeout(600)
        shot(page, 3)

        # 点下一步 / 获取验证码
        clicked = False
        for text in ("下一步", "获取验证码", "获取", "登录", "确定"):
            try:
                btn = page.get_by_text(text, exact=False).last
                if btn.is_visible():
                    btn.click(); clicked = True
                    log(f"[3] 点击「{text}」"); break
            except Exception: continue
        if not clicked:
            log("!! 未找到下一步按钮"); shot(page, 3); return 1
        page.wait_for_timeout(1500)
        # 隐私协议弹窗（服务协议及隐私保护）→ 同意
        try:
            agree = page.get_by_text("同意", exact=False).last
            if agree.is_visible():
                agree.click(); log("[3] 点击「同意」隐私弹窗"); page.wait_for_timeout(1500)
        except Exception: pass
        shot(page, 4)

        # 可能跳到验证码页：再点「获取验证码」
        for _ in range(3):
            try:
                btn = page.get_by_text("获取验证码", exact=False).last
                if btn.is_visible():
                    btn.click(); log("[4] 点击「获取验证码」"); page.wait_for_timeout(1500)
                    break
            except Exception: continue
        # 再次处理可能的二次弹窗
        try:
            agree = page.get_by_text("同意", exact=False).last
            if agree.is_visible():
                agree.click(); log("[4] 二次「同意」"); page.wait_for_timeout(1500)
        except Exception: pass
        shot(page, 5)

        # 滑块
        log("[5] 等待滑块…")
        st = wait_captcha_and_solve(page, cdp)
        log(f"[5] 滑块结果: {st}")
        if "solved" not in st:
            log(f"!! 未能通过滑块/智能检测（{st}），中止本轮（未发短信则无消耗）")
            return 1
        log("WAITING_FOR_CODE  短信应已发送，自动从 chat.db 读取")
        shot(page, 6)

        # 等验证码：chat.db 自动读（若 TCC 已授权），sms_code.txt 文件兜底
        code = None
        t_wait = time.time() - 30     # 只认此刻之后收到的新短信
        log("[6] 等待验证码（chat.db 自动 / sms_code.txt 手动）…")
        for _ in range(300):
            code = read_sms_code(t_wait)
            if code:
                log(f"[6] chat.db 读到验证码 {code}")
                break
            try:
                c2 = open(CODE_FILE).read().strip()
                if re.fullmatch(r"\d{6}", c2):
                    code = c2; log(f"[6] 文件读到验证码 {code}"); break
            except Exception: pass
            time.sleep(1)
        if not code:
            log("!! 等待验证码超时"); return 1

        # 填验证码
        ok = False
        for sel in ("input[type=number]", "input[placeholder*='验证码']",
                    "input[maxlength='6']", "input"):
            try:
                el = page.locator(sel).first
                if el.is_visible():
                    el.fill(code); ok = True; log(f"[6] 验证码已填入 {sel}"); break
            except Exception: continue
        if not ok:
            log("!! 未找到验证码输入框"); shot(page, 7); return 1
        page.wait_for_timeout(400)
        for text in ("登录", "下一步", "确定"):
            try:
                btn = page.get_by_text(text, exact=False).last
                if btn.is_visible(): btn.click(); log(f"[6] 点击「{text}」"); break
            except Exception: continue
        page.wait_for_timeout(5000)
        shot(page, 8)
        log("URL:", page.url)

        # 兜底：等 app-done / token
        for _ in range(20):
            if net["token"]: break
            page.wait_for_timeout(1000)
        if not net["token"]:
            log("!! 未抓到 /api/token（可能未完成跳转），导出 cookies 兜底")

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        tok = net["token"] or {}
        out = {
            "bearer": tok.get("access_token") or tok.get("id_token"),
            "refresh_token": tok.get("refresh_token"),
            "device_id": net["device_id"],
            "cookies": cookies,
            "url_final": page.url,
        }
        json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
        log(f"[7] 会话已存 {OUT}")
        log(f"    device_id={net['device_id']} sso_token={'sso_token' in cookies} "
            f"bearer={str(out['bearer'])[:20]}...")
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
