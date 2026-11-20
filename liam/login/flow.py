"""网页一键重登核心（M5/D4，方案 §5.3）——移植自 spike browser_login.py
（2026-11-19 实测一次过：滑块缺口检测+轨迹拖拽；「智能检测中」重试即可）。

三步 UX：
  ① start(phone)：专用线程跑 打开登录页→勾协议→填手机号→点获取验证码
    →滑块自动解→短信发出 → state=waiting_code → 阻塞等码队列（5min 超时）
  ② submit_code(code)：队列传码，同线程继续 填码→登录→抓 /api/token+cookies
    → 存 SessionStore → state=done
  ③ status()：step/日志/诊断截图（供面板展示）

★ Playwright sync 对象绑定创建线程 —— 全流程必须单线程贯穿（queue 传码）。
★ 风控纪律：短信 ~2 条/10min —— start 前 UI 限流（routes 层 10min 冷却）；
  失败自动重试 ≤3 次全程不发第二条短信（滑块失败发生在发短信前，无消耗）。
"""

from __future__ import annotations

import base64
import io
import json
import queue
import re
import secrets as _secrets
import threading
import time
import urllib.request
from collections import deque

from ..auth.session_store import (
    K_BEARER, K_COOKIES, K_DEVICE, K_REFRESH, SessionStore)

UA = ("Mozilla/5.0 (Linux; Android 14; sdk_gphone64_arm64 Build/UE1A.230829.050; wv) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile "
      "Safari/537.36 Livis/2.9.0")
ENTRY = ("https://account.lixiang.com/app-auth/livis?client_id=40amUDKOdqQTaGDONZC1oY"
         "&audience=5iIapSfVJlln0vU0OzUCH9&scope=iam%3Aclient%3Atype%3Alisa"
         "&device_id={dev}&prompt=login&required_scope=iam%3Aclient%3Atype%3Alisa")

S_IDLE = "idle"
S_RUNNING = "running"           # 打开页/填号/滑块进行中
S_WAITING_CODE = "waiting_code"  # 短信已发，等用户输码
S_SUBMITTING = "submitting"
S_DONE = "done"
S_FAILED = "failed"

CODE_WAIT_TIMEOUT = 300   # 等用户输码 5min

# ---------------- 滑块求解（spike 原样移植）----------------
GET_IMGS = r"""(() => {
  const cv=document.querySelector('.dx_captcha_basic_pic canvas') || document.querySelector('canvas');
  const piece=document.querySelector('.dx_captcha_basic_sub-slider img');
  const sub=[...document.querySelectorAll('.dx_captcha_basic_sub-slider')].find(e=>e.getBoundingClientRect().width>0);
  const sl=[...document.querySelectorAll('.dx_captcha_basic_slider')].find(e=>e.getBoundingClientRect().width>0);
  const r=e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height}};
  return JSON.stringify({canvas:cv?cv.toDataURL('image/png'):null, cvr:cv?r(cv):null,
    piece:piece?piece.src:null, sub:sub?r(sub):null, slider:sl?r(sl):null,
    ok:(()=>{const e=[...document.querySelectorAll('.dx_captcha_basic_bar-success')].find(x=>x.getBoundingClientRect().width>0);return e?getComputedStyle(e).display!=='none':false;})(),
    fail:(()=>{const e=[...document.querySelectorAll('.dx_captcha_basic_bar-fail')].find(x=>x.getBoundingClientRect().width>0);return e?getComputedStyle(e).display!=='none':false;})()});
})()"""

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


def find_hole(bg):
    import numpy as np
    from PIL import Image, ImageFilter
    m = np.asarray(Image.fromarray(bg.astype(np.uint8))
                   .filter(ImageFilter.BoxBlur(3))).astype(float)
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


class LoginFlow:
    """单例登录流（进程内同时只允许一个；防短信浪费）。"""

    def __init__(self, store: SessionStore, headless: bool = True) -> None:
        self.store = store
        self.headless = headless
        self._lock = threading.Lock()
        self._state = S_IDLE
        self._log: list[str] = []
        self._error = ""
        self._shot_b64 = ""
        self._thread: threading.Thread | None = None
        self._code_q: queue.Queue[str] = queue.Queue()
        self._phone = ""

    # ------------------------------------------------------------------
    # 对外接口（线程安全）
    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {"state": self._state, "log": list(self._log[-12:]),
                    "error": self._error, "screenshot": self._shot_b64,
                    "phone_tail": self._phone[-4:] if self._phone else ""}

    def start(self, phone: str) -> tuple[bool, str]:
        """① 发起登录（到短信发出为止）。"""
        with self._lock:
            if self._state in (S_RUNNING, S_WAITING_CODE, S_SUBMITTING):
                return False, "登录流程已在进行中"
            self._state = S_RUNNING
            self._log = []
            self._error = ""
            self._shot_b64 = ""
            self._phone = phone
        self._code_q = queue.Queue()
        self._thread = threading.Thread(
            target=self._run_flow, args=(phone,), daemon=True, name="liam-login")
        self._thread.start()
        return True, "started"

    def submit_code(self, code: str) -> tuple[bool, str]:
        """② 用户输入短信码（仅 waiting_code 态接受）。"""
        with self._lock:
            if self._state != S_WAITING_CODE:
                return False, f"当前状态 {self._state} 不可提交验证码"
        if not re.fullmatch(r"\d{4,8}", code):
            return False, "验证码格式不对"
        self._code_q.put(code)
        return True, "submitted"

    # ------------------------------------------------------------------
    def _set(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            if error:
                self._error = error

    def _logp(self, msg: str) -> None:
        with self._lock:
            self._log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _shot(self, page) -> None:
        try:
            png = page.screenshot()
            with self._lock:
                self._shot_b64 = base64.b64encode(png).decode()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 浏览器线程主体（spike 流程两段式：→ 短信发出；→ 码提交完）
    # ------------------------------------------------------------------
    def _run_flow(self, phone: str) -> None:
        try:
            self._run_flow_inner(phone)
        except Exception as e:  # noqa: BLE001
            self._set(S_FAILED, f"{type(e).__name__}: {e}")
            self._logp(f"!! 流程异常 {e!r}")
        finally:
            with self._lock:
                if self._state not in (S_DONE, S_FAILED):
                    self._state = S_FAILED

    def _run_flow_inner(self, phone: str) -> None:
        from playwright.sync_api import sync_playwright
        net: dict = {"device_id": None, "token": None}

        def on_req(req):
            try:
                if "id.lixiang.com" not in req.url or req.method != "POST":
                    return
                b = req.post_data or ""
                m = re.search(r"device_id=([0-9a-f]{32})", b)
                if m and not net["device_id"]:
                    net["device_id"] = m.group(1)
            except Exception:  # noqa: BLE001
                pass

        def on_resp(resp):
            try:
                if resp.url.endswith("/api/token") and resp.status == 200:
                    net["token"] = resp.json()
                    self._logp("[net] /api/token 已捕获")
            except Exception:  # noqa: BLE001
                pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless,
                                         args=["--window-size=480,900"])
            ctx = browser.new_context(
                user_agent=UA, viewport={"width": 412, "height": 732},
                is_mobile=True, has_touch=True, locale="zh-CN")
            page = ctx.new_page()
            page.on("request", on_req)
            page.on("response", on_resp)
            cdp = ctx.new_cdp_session(page)

            dev = _secrets.token_hex(16)
            ctx.add_cookies([
                {"name": "X-LX-Deviceid", "value": dev,
                 "domain": "account.lixiang.com", "path": "/"},
                {"name": "X-LX-Deviceid", "value": dev,
                 "domain": "id.lixiang.com", "path": "/"},
                {"name": "authli_device_id", "value": dev,
                 "domain": "account.lixiang.com", "path": "/"},
                {"name": "authli_device_id", "value": dev,
                 "domain": "id.lixiang.com", "path": "/"},
                {"name": "isapp", "value": "1",
                 "domain": "account.lixiang.com", "path": "/"},
            ])
            self._logp(f"[1] 打开登录页 dev={dev[:8]}…")
            page.goto(ENTRY.format(dev=dev), wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_timeout(3500)
            if "/identifier" not in page.url:
                page.goto("https://account.lixiang.com/login/app/livis/identifier",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)

            # 勾协议 + 填手机号（spike 配方：文本坐标左 22px 点圆圈）
            try:
                page.evaluate("""(() => {
                  for (const c of document.querySelectorAll('input[type=checkbox]')) {
                    if (c.offsetParent && !c.checked) c.click(); }})()""")
            except Exception:  # noqa: BLE001
                pass
            filled = False
            for sel in ("input[type=tel]", "input[placeholder*='手机']",
                        "input[placeholder*='电话']", "input:not([type=password])"):
                try:
                    el = page.locator(sel).first
                    if el.is_visible():
                        el.fill(phone)
                        filled = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not filled:
                self._shot(page)
                self._set(S_FAILED, "未找到手机号输入框")
                return
            page.wait_for_timeout(600)
            rect = page.evaluate(r"""(() => {
              for (const n of document.querySelectorAll('*')) {
                if (n.childElementCount) continue;
                const t = (n.textContent || '').trim();
                if (t.includes('我已阅读并同意')) {
                  const r = n.getBoundingClientRect();
                  return {x: r.x, y: r.y, w: r.width, h: r.height}; }}
              return null;})()""")
            if rect:
                page.mouse.click(rect["x"] - 22, rect["y"] + rect["h"] / 2)
            page.wait_for_timeout(600)

            # 下一步 / 获取验证码 / 隐私弹窗（spike 全序列）
            for text in ("下一步", "获取验证码", "获取", "登录", "确定"):
                try:
                    btn = page.get_by_text(text, exact=False).last
                    if btn.is_visible():
                        btn.click()
                        self._logp(f"[2] 点击「{text}」")
                        break
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(1500)
            for agree_try in range(2):
                try:
                    agree = page.get_by_text("同意", exact=False).last
                    if agree.is_visible():
                        agree.click()
                        self._logp(f"[2] 「同意」弹窗 #{agree_try + 1}")
                        page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass
            for _ in range(3):
                try:
                    btn = page.get_by_text("获取验证码", exact=False).last
                    if btn.is_visible():
                        btn.click()
                        self._logp("[3] 点击「获取验证码」")
                        page.wait_for_timeout(1500)
                        break
                except Exception:  # noqa: BLE001
                    continue
                page.wait_for_timeout(1200)

            # 滑块（spike 全套：缺口检测 + 轨迹拖拽 + 智能检测重试）
            self._logp("[4] 等待滑块/智能检测…")
            st = self._wait_captcha_and_solve(page, cdp)
            self._logp(f"[4] 滑块结果: {st}")
            self._shot(page)
            if "solved" not in st:
                # 滑块失败发生在发短信前 —— 无短信消耗，直接失败
                self._set(S_FAILED, f"滑块未通过（{st}）——点重试再来一次")
                browser.close()
                return

            # ★ 短信已发出 —— 等用户输码（网页面板 submit_code）
            self._set(S_WAITING_CODE)
            self._logp("[5] 短信已发送，等待网页输入验证码…")
            try:
                code = self._code_q.get(timeout=CODE_WAIT_TIMEOUT)
            except queue.Empty:
                self._set(S_FAILED, "等验证码超时（5min）——重新发起")
                browser.close()
                return
            self._set(S_SUBMITTING)

            # 填码提交（spike 配方）
            ok = False
            for sel in ("input[type=number]", "input[placeholder*='验证码']",
                        "input[maxlength='6']", "input"):
                try:
                    el = page.locator(sel).first
                    if el.is_visible():
                        el.fill(code)
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not ok:
                self._shot(page)
                self._set(S_FAILED, "未找到验证码输入框")
                browser.close()
                return
            page.wait_for_timeout(400)
            for text in ("登录", "下一步", "确定"):
                try:
                    btn = page.get_by_text(text, exact=False).last
                    if btn.is_visible():
                        btn.click()
                        break
                except Exception:  # noqa: BLE001
                    continue
            for _ in range(20):
                if net["token"]:
                    break
                page.wait_for_timeout(1000)
            self._shot(page)

            # 落库（★ M0 修复教训：全部走 SessionStore 正常通道）
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            tok = net["token"] or {}
            bearer = tok.get("access_token") or tok.get("id_token")
            if not (bearer or tok.get("refresh_token")):
                self._set(S_FAILED, "未抓到 /api/token（登录可能未完成）")
                browser.close()
                return
            self.store.save(K_BEARER, bearer)
            if tok.get("refresh_token"):
                self.store.save(K_REFRESH, tok["refresh_token"])
            self.store.save(K_COOKIES, cookies)
            if net["device_id"]:
                self.store.save(K_DEVICE, net["device_id"])
            self._set(S_DONE)
            self._logp(f"[6] 完成：sso_token={'sso_token' in cookies} "
                       f"device={str(net['device_id'])[:8]}…")
            browser.close()

    # ------------------------------------------------------------------
    # 滑块（spike 原样，仅日志走 _logp）
    # ------------------------------------------------------------------
    def _captcha_result(self, page):
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

    def _solve_once(self, page, cdp) -> tuple[bool, str]:
        import numpy as np
        from PIL import Image
        d = json.loads(page.evaluate(GET_IMGS))
        if d.get("canvas") is None:
            r, _ = self._captcha_result(page)
            return (True, "already_solved") if r == "solved" else (False, "no_captcha")
        if d.get("ok"):
            return True, "already_ok"
        sub, sl, cvr = d.get("sub"), d.get("slider"), d.get("cvr")
        if not sub or not sl:
            return False, "layout_missing"
        bg = np.asarray(Image.open(io.BytesIO(base64.b64decode(
            d["canvas"].split(",", 1)[1]))).convert("L")).astype(float)
        u = d.get("piece")
        if not u:
            return False, "no_piece"
        if u.startswith("data:"):
            raw = base64.b64decode(u.split(",", 1)[1])
        else:
            rq = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(rq, timeout=10).read()
            self._logp(f"    [solver] 拼图块已下载 {len(raw)}B")
        H, W = bg.shape
        scale = cvr["w"] / W
        hole = find_hole(bg)
        comps = [c for c in hole if 30 < (c["x1"] - c["x0"]) < 120]
        if not comps:
            return False, "no_hole"
        piece_y_cv = sub["y"] + sub["h"] / 2
        exp_y = (piece_y_cv - cvr["y"]) / (cvr["h"] / H)
        cand = [c for c in comps if abs(c["cy"] - exp_y) < 50] or comps
        h = min(cand, key=lambda z: z["mean"])
        hole_cx_css = cvr["x"] + h["cx"] * scale
        piece_cx_css = sub["x"] + sub["w"] / 2
        dx = hole_cx_css - piece_cx_css
        hx, hy = sl["x"] + sl["w"] / 2, sl["y"] + sl["h"] / 2
        self._logp(f"    [solver] dx={dx:.0f}px")
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
        r, st = self._captcha_result(page)
        if r == "solved":
            return True, "solved"
        if r == "failed":
            try:
                page.evaluate("""(() => {
                  const b=[...document.querySelectorAll('.dx_captcha_basic_refresh, .dx_captcha_basic-btn-refresh')]
                    .find(e=>e.getBoundingClientRect().width>0); if(b){b.click();return true;} return false;})()""")
                time.sleep(1.5)
            except Exception:  # noqa: BLE001
                pass
            return False, "failed_retry"
        time.sleep(2.0)
        r2, _ = self._captcha_result(page)
        if r2 == "solved":
            return True, "solved_late"
        return False, f"state_{r}"

    def _wait_captcha_and_solve(self, page, cdp, max_auto: int = 6) -> str:
        for _ in range(120):
            r, st = self._captcha_result(page)
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
            ok, why = self._solve_once(page, cdp)
            self._logp(f"    [solver#{i + 1}] {ok} {why}")
            if ok:
                return "solved"
            if why == "no_captcha":
                return "no_captcha"
            if why == "already_solved":
                return "solved"
            for _ in range(12):
                d = json.loads(page.evaluate(GET_IMGS))
                if d.get("canvas") and d.get("sub") and d.get("slider"):
                    break
                r, st = self._captcha_result(page)
                if r == "solved":
                    return "solved_late"
                time.sleep(1)
        return "timeout"
