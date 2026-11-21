"""网页账号登录（短信验证码 + 自动完成滑块，M5/D4）。

三步 UX：
  ① start(phone)：专用线程跑 打开登录页→勾协议→填手机号→点获取验证码
    →自动完成滑块→短信发出 → state=waiting_code → 阻塞等码队列（5min 超时）
  ② submit_code(code)：队列传码，同线程继续 填码→登录→抓 /api/token+cookies
    → 存 SessionStore → state=done
  ③ status()：step/日志/诊断截图（供面板展示）

★ Playwright sync 对象绑定创建线程 —— 全流程必须单线程贯穿（queue 传码）。
★ 滑块在发短信前完成；滑块失败不消耗短信，可直接重试。只有短信真正发出后
  才进入 10 分钟冷却（实测 ~2 条/10min），避免风控与误伤用户。
★ 风控纪律：失败自动重试 ≤8 次全程不发第二条短信；找不到滑块但页面已进入
  验证码步骤时，按“无需滑块”继续。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import queue
import random
import re
import secrets as _secrets
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from ..auth.session_store import SessionStore
from .captcha import build_drag_track, decode_piece, find_hole, locate_dark_slot, locate_slot

__all__ = [
    "CODE_WAIT_TIMEOUT",
    "DEPENDENCY_HINT",
    "SMS_COOLDOWN_S",
    "S_DONE",
    "S_FAILED",
    "S_IDLE",
    "S_RECOVERING",
    "S_RUNNING",
    "S_SUBMITTING",
    "S_WAITING_CODE",
    "LoginFlow",
    "dependency_problem",
    "find_hole",
]

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
S_RECOVERING = "recovering"      # 凭据已落库，服务正在恢复会话
S_DONE = "done"
S_FAILED = "failed"

CODE_WAIT_TIMEOUT = 300   # 等用户输码 5min
SMS_COOLDOWN_S = 600      # 短信发出后的冷却（~2 条/10min）
CAPTCHA_WAIT_S = 60       # 等滑块组件出现
MAX_CAPTCHA_ATTEMPTS = 8  # 单次登录内自动滑块重试次数（不发短信，成本低）

# 依赖缺失时给面板的明确修复指引
DEPENDENCY_HINT = ("未安装账号登录组件。请执行：pip install -e '.[login]' "
                   "&& python -m playwright install chromium")

# ---------------- 滑块页面脚本 ----------------
GET_IMGS = r"""(() => {
  const vis = e => { try { return e && e.getBoundingClientRect().width > 0 &&
    getComputedStyle(e).display !== 'none'; } catch(x) { return false; } };
  const cv=[...document.querySelectorAll('.dx_captcha_basic_pic canvas')].find(vis)
    || [...document.querySelectorAll('canvas')].find(vis)
    || document.querySelector('.dx_captcha_basic_pic canvas');
  const piece=[...document.querySelectorAll('.dx_captcha_basic_sub-slider img')].find(vis)
    || document.querySelector('.dx_captcha_basic_sub-slider img');
  const sub=[...document.querySelectorAll('.dx_captcha_basic_sub-slider')].find(vis);
  const sl=[...document.querySelectorAll('.dx_captcha_basic_slider')].find(vis);
  const r=e=>{const b=e.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height}};
  const toData=img=>{try{
    const c=document.createElement('canvas');
    c.width=img.naturalWidth||img.width; c.height=img.naturalHeight||img.height;
    if(!c.width||!c.height) return null;
    c.getContext('2d').drawImage(img,0,0); return c.toDataURL('image/png');
  }catch(e){return null;}};
  return JSON.stringify({canvas:cv?cv.toDataURL('image/png'):null, cvr:cv?r(cv):null,
    piece:piece?piece.src:null, pieceData:piece?toData(piece):null,
    pieceNat:piece?{w:piece.naturalWidth,h:piece.naturalHeight}:null,
    pr:piece?r(piece):null,
    dpr:window.devicePixelRatio||1,
    sub:sub?r(sub):null, slider:sl?r(sl):null,
    ok:(()=>{const e=[...document.querySelectorAll('.dx_captcha_basic_bar-success')].find(vis);return e?getComputedStyle(e).display!=='none':false;})(),
    fail:(()=>{const e=[...document.querySelectorAll('.dx_captcha_basic_bar-fail')].find(vis);return e?getComputedStyle(e).display!=='none':false;})()});
})()"""

CAPTCHA_STATE = r"""(() => {
  const vis = e => { try { return e && e.getBoundingClientRect().width > 0 &&
    getComputedStyle(e).display !== 'none'; } catch(x) { return false; } };
  const wrap = [...document.querySelectorAll('.dx_captcha_basic_wrapper')].find(vis);
  const ok = [...document.querySelectorAll('.dx_captcha_basic_bar-success')].find(vis);
  const fail = [...document.querySelectorAll('.dx_captcha_basic_bar-fail')].find(vis);
  const body = document.body.innerText || '';
  const codeInput = [...document.querySelectorAll('input')].some(
    i => vis(i) && ((i.placeholder || '').includes('验证码')
      || (i.getAttribute('aria-label') || '').includes('验证码')));
  return JSON.stringify({
    widget: !!wrap, ok: !!ok, fail: !!fail,
    detecting: /智能检测/.test(body),
    countdown: /重新发送|后重发|重新获取/.test(body),
    sent: /验证码已发送|已发送至|短信已发送/.test(body),
    codeError: /验证码错误|验证码有误|验证码不正确|验证码已失效/.test(body),
    codeInput, text: body.slice(0, 160), url: location.href
  });
})()"""

REFRESH_CAPTCHA = r"""(() => {
  const vis = e => { try { return e && e.getBoundingClientRect().width > 0 &&
    getComputedStyle(e).display !== 'none'; } catch(x) { return false; } };
  const b=[...document.querySelectorAll(
    '.dx_captcha_basic_refresh, .dx_captcha_basic-btn-refresh, .dx_captcha_basic_refresh-icon'
  )].find(vis);
  if(b){b.click();return true;} return false;
})()"""


def dependency_problem() -> str:
    """检查登录可选项是否安装；返回空串表示可用。"""
    missing = [name for name in ("playwright", "numpy", "PIL")
               if importlib.util.find_spec(name) is None]
    return f"{DEPENDENCY_HINT}（缺少：{'、'.join(missing)}）" if missing else ""


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
        self._sms_sent_at = 0.0
        self._captcha_attempts = 0
        self._frame = None
        self._drag_touch = True
        self._generation = 0
        self.on_saved: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # 对外接口（线程安全）
    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "log": list(self._log[-14:]),
                "error": self._error,
                "screenshot": self._shot_b64,
                "phone_tail": self._phone[-4:] if self._phone else "",
                "sms_sent": bool(self._sms_sent_at),
                "sms_sent_at": self._sms_sent_at or None,
                "cooldown_left": self._cooldown_left_locked(),
                "captcha_attempts": self._captcha_attempts,
            }

    def cooldown_left(self) -> int:
        with self._lock:
            return self._cooldown_left_locked()

    def _cooldown_left_locked(self) -> int:
        if not self._sms_sent_at:
            return 0
        return max(0, round(SMS_COOLDOWN_S - (time.time() - self._sms_sent_at) + 0.49))

    @staticmethod
    def dependency_problem() -> str:
        return dependency_problem()

    def start(self, phone: str) -> tuple[bool, str]:
        """① 发起登录（到短信发出为止）。"""
        phone = re.sub(r"\s+", "", phone or "")
        if not re.fullmatch(r"\d{11}", phone):
            return False, "手机号应为 11 位数字"
        problem = dependency_problem()
        if problem:
            return False, problem
        with self._lock:
            if self._state in (S_RUNNING, S_WAITING_CODE, S_SUBMITTING, S_RECOVERING):
                return False, "登录流程已在进行中"
            left = self._cooldown_left_locked()
            if left > 0:
                return False, f"短信发送冷却中，请在 {left} 秒后重试"
            self._state = S_RUNNING
            self._log = []
            self._error = ""
            self._shot_b64 = ""
            self._phone = phone
            self._sms_sent_at = 0.0
            self._captcha_attempts = 0
            self._frame = None
            self._drag_touch = True
            self._generation += 1
            generation = self._generation
        self._code_q = queue.Queue()
        self._thread = threading.Thread(
            target=self._run_flow, args=(phone, generation),
            daemon=True, name="liam-login")
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
            self._error = error

    def credentials_saved(self) -> None:
        notify = self.on_saved
        self._set(S_RECOVERING if notify else S_DONE)
        if notify:
            notify()

    def _mark_sms_sent(self) -> None:
        with self._lock:
            self._sms_sent_at = time.time()

    def _logp(self, msg: str) -> None:
        with self._lock:
            self._log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _shot(self, page) -> None:
        try:
            png = page.screenshot()
            with self._lock:
                self._shot_b64 = base64.b64encode(png).decode()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # 浏览器线程主体（spike 流程两段式：→ 短信发出；→ 码提交完）
    # ------------------------------------------------------------------
    def _run_flow(self, phone: str, generation: int) -> None:
        try:
            self._run_flow_inner(phone)
        except Exception as e:  # noqa: BLE001
            if self._generation == generation:
                self._set(S_FAILED, f"{type(e).__name__}: {e}")
                self._logp(f"!! 流程异常 {e!r}")
        finally:
            with self._lock:
                if (self._generation == generation
                        and self._state in (S_RUNNING, S_WAITING_CODE, S_SUBMITTING)):
                    self._state = S_FAILED

    def _run_flow_inner(self, phone: str) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._set(S_FAILED, dependency_problem() or DEPENDENCY_HINT)
            return
        net: dict = {"device_id": None, "token": None}

        def on_req(req):
            try:
                if req.method != "POST" or "lixiang.com" not in req.url:
                    return
                b = req.post_data or ""
                m = re.search(r'(?:device_id=|"device_id"\s*:\s*")([0-9a-f]{32})', b)
                if m and not net["device_id"]:
                    net["device_id"] = m.group(1)
            except Exception:  # noqa: BLE001, S110
                pass

        def on_resp(resp):
            try:
                if "lixiang.com" not in resp.url:
                    return
                if "/api/token" in resp.url and resp.status == 200:
                    data = resp.json()
                    if isinstance(data, dict) and data:
                        net["token"] = data
                        self._logp("[net] /api/token 已捕获")
            except Exception:  # noqa: BLE001, S110
                pass

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=self.headless, args=["--window-size=480,900"])
            except Exception as exc:  # noqa: BLE001
                self._set(
                    S_FAILED,
                    "无法启动 Chromium。容器需要 libatomic1；或在宿主机执行 "
                    f"python -m playwright install chromium。原始错误：{exc}")
                return
            try:
                self._run_page_flow(browser, phone, net, on_req, on_resp)
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    def _run_page_flow(self, browser, phone: str, net: dict, on_req, on_resp) -> None:
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
        page.goto(ENTRY.format(dev=dev), wait_until="domcontentloaded", timeout=30000)
        # app-auth 入口会先停在原 URL 做会话初始化，约 4-5 秒后才跳到
        # /login/app/livis/identifier。不要过早强制跳转，否则会打断会话
        # 初始化并落到「会话已过期，重新加载中…」页面。
        phone_selector = ("input[type=tel], input[placeholder*='手机'], "
                          "input[placeholder*='电话']")
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            try:
                if page.locator(phone_selector).first.is_visible():
                    ready = True
                    break
            except Exception:  # noqa: BLE001, S110
                pass
            page.wait_for_timeout(500)
        if not ready and "/identifier" not in page.url:
            page.goto("https://account.lixiang.com/login/app/livis/identifier",
                      wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector(phone_selector, state="visible", timeout=10000)
                ready = True
            except Exception:  # noqa: BLE001, S110
                pass
        if not ready:
            self._shot(page)
            self._set(S_FAILED, "登录页未加载出手机号输入框（可能被风控或网络超时）")
            return

        # 勾协议 + 填手机号（spike 配方：文本坐标左 22px 点圆圈）
        try:
            page.evaluate("""(() => {
              for (const c of document.querySelectorAll('input[type=checkbox]')) {
                if (c.offsetParent && !c.checked) c.click(); }})()""")
        except Exception:  # noqa: BLE001, S110
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
            except Exception:  # noqa: BLE001, S112
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
            except Exception:  # noqa: BLE001, S112
                continue
        page.wait_for_timeout(1500)
        for agree_try in range(2):
            try:
                agree = page.get_by_text("同意", exact=False).last
                if agree.is_visible():
                    agree.click()
                    self._logp(f"[2] 「同意」弹窗 #{agree_try + 1}")
                    page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001, S110
                pass
        for _ in range(3):
            try:
                btn = page.get_by_text("获取验证码", exact=False).last
                if btn.is_visible():
                    btn.click()
                    self._logp("[3] 点击「获取验证码」")
                    page.wait_for_timeout(1500)
                    break
            except Exception:  # noqa: BLE001, S112
                continue
            page.wait_for_timeout(1200)

        # 滑块（缺口检测 + 模板匹配 + 轨迹拖拽 + 智能检测重试）
        self._logp("[4] 等待滑块/智能检测…")
        st = self._wait_captcha_and_solve(page, cdp)
        self._logp(f"[4] 滑块结果: {st}")
        self._shot(page)
        self._frame = None  # 滑块结束后强制重新选择页面 frame
        if not any(word in st for word in ("solved", "no_captcha")):
            self._set(S_FAILED, f"滑块未通过（{st}）——未发送短信，可直接重试")
            return

        # 滑块通过通常意味着“发送验证码”请求已提交；先按已发出计冷却，
        # 避免页面文案变化导致用户连续触发短信。
        if "solved" in st:
            self._mark_sms_sent()
        # 无滑块时也要确认页面是否已进入验证码步骤，避免把成功误判成失败。
        confirmed = self._wait_for_sms_sent(page, 15 if "solved" in st else 10)
        if not confirmed:
            if "no_captcha" in st:
                self._shot(page)
                self._set(S_FAILED, "未出现滑块，也未检测到短信发送确认——可直接重试")
                return
            self._logp("[5] 未检测到短信发送文案，继续等待验证码（页面文案可能不同）")

        # ★ 短信已发出 —— 等用户输码（网页面板 submit_code）
        code_deadline = time.time() + CODE_WAIT_TIMEOUT
        for code_try in range(1, 4):
            self._mark_sms_sent()
            self._set(S_WAITING_CODE)
            self._logp("[5] 短信已发送，等待网页输入验证码…" if code_try == 1
                       else f"[5] 请重新输入验证码（第 {code_try} 次）")
            try:
                code = self._code_q.get(
                    timeout=max(1, int(code_deadline - time.time())))
            except queue.Empty:
                self._set(S_FAILED, "等验证码超时（5min）——重新发起")
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
                except Exception:  # noqa: BLE001, S112
                    continue
            if not ok:
                self._shot(page)
                self._set(S_FAILED, "未找到验证码输入框")
                return
            page.wait_for_timeout(400)
            for text in ("登录", "下一步", "验证", "提交", "确定"):
                try:
                    btn = page.get_by_text(text, exact=False).last
                    if btn.is_visible():
                        btn.click()
                        break
                except Exception:  # noqa: BLE001, S112
                    continue
            for _ in range(30):
                if net["token"]:
                    break
                page.wait_for_timeout(1000)
            self._shot(page)
            if net["token"]:
                break
            # 验证码错误时不重发短信，直接让用户再输一次（最多 3 次）。
            try:
                state = self._captcha_state(page)
            except Exception:  # noqa: BLE001
                state = {}
            if code_try < 3 and state.get("codeInput") and state.get("codeError"):
                self._logp(f"[6] 验证码校验失败（第 {code_try} 次），可重新输入")
                continue
            break

        # 兜底：部分重定向把 token 放在 URL fragment/query 里，网络监听可能错过。
        if not net["token"]:
            try:
                href = page.evaluate("location.href")
                parts = urllib.parse.urlparse(href)
                params = urllib.parse.parse_qs(parts.query)
                for key, values in urllib.parse.parse_qs(parts.fragment).items():
                    params.setdefault(key, values)
                access = (params.get("access_token") or [""])[0]
                if access:
                    net["token"] = {
                        "access_token": access,
                        "refresh_token": (params.get("refresh_token") or [""])[0],
                    }
                    self._logp("[net] 从页面 URL 捕获 token")
            except Exception:  # noqa: BLE001, S110
                pass

        # 落库（★ M0 修复教训：全部走 SessionStore 正常通道）
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        tok = net["token"] or {}
        bearer = tok.get("access_token") or tok.get("id_token")
        if not (bearer or tok.get("refresh_token")):
            self._logp(f"[6] 未抓到 token，页面 URL={page.url[:120]}")
            self._set(S_FAILED, "未抓到 /api/token（登录可能未完成）")
            return
        self.store.save_login(bearer, tok.get("refresh_token"), cookies, net["device_id"])
        self.credentials_saved()
        self._logp(f"[6] 完成：sso_token={'sso_token' in cookies} "
                   f"device={str(net['device_id'])[:8]}…")

    # ------------------------------------------------------------------
    # 页面/滑块（spike 原样，仅日志走 _logp）
    # ------------------------------------------------------------------
    def _captcha_frame_for(self, page):
        """滑块可能在 iframe 内；优先复用仍存活的子 frame。"""
        cached = self._frame
        if cached is not None and cached != page.main_frame:
            try:
                if not cached.is_detached():
                    return cached
            except Exception:  # noqa: BLE001, S110
                pass
        for frame in [page.main_frame, *page.frames]:
            try:
                if frame.evaluate(
                        "(() => { const vis = e => { try { return e && "
                        "e.getBoundingClientRect().width > 0 && "
                        "getComputedStyle(e).display !== 'none'; } catch (x) { return false; } }; "
                        "return [...document.querySelectorAll('.dx_captcha_basic_wrapper, "
                        ".dx_captcha_basic_pic canvas, .dx_captcha_basic_sub-slider')].some(vis); })()"):
                    self._frame = frame
                    return frame
            except Exception:  # noqa: BLE001, S112
                continue
        self._frame = page.main_frame
        return page.main_frame

    def _frame_offset(self, page) -> tuple[float, float]:
        frame = self._captcha_frame_for(page)
        if frame == page.main_frame:
            return 0.0, 0.0
        try:
            box = frame.frame_element().bounding_box()
            if box:
                return float(box["x"]), float(box["y"])
        except Exception:  # noqa: BLE001, S110
            pass
        return 0.0, 0.0

    def _eval(self, page, script: str):
        return self._captcha_frame_for(page).evaluate(script)

    def _captcha_state(self, page) -> dict:
        return json.loads(self._eval(page, CAPTCHA_STATE))

    def _captcha_result(self, page):
        d = self._captcha_state(page)
        if d["ok"]:
            return "solved", d
        if d["detecting"]:
            return "detecting", d
        if not d["widget"] and (d["countdown"] or d["codeInput"] or d["sent"]):
            return "solved", d
        if d["fail"]:
            return "failed", d
        if not d["widget"]:
            return "gone", d
        return "present", d

    def _wait_for_sms_sent(self, page, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                d = self._captcha_state(page)
                if d.get("codeInput") or d.get("countdown") or d.get("sent"):
                    return True
            except Exception:  # noqa: BLE001, S110
                pass
            time.sleep(0.4)
        return False

    def _load_piece(self, page, data: dict):
        """取拼图块图像，返回 (灰度图, 掩码, 可见区域占比)。"""
        raw = None
        for url in (data.get("pieceData"), data.get("piece")):
            if not url:
                continue
            if url.startswith("data:"):
                try:
                    raw = base64.b64decode(url.split(",", 1)[1])
                    break
                except Exception:  # noqa: BLE001, S112
                    continue
            target = url if url.startswith("http") else f"https:{url}"
            try:
                resp = page.context.request.get(
                    target, headers={"Referer": page.url}, timeout=15000)
                if resp.ok:
                    raw = resp.body()
                    break
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                req = urllib.request.Request(
                    target, headers={"User-Agent": "Mozilla/5.0", "Referer": page.url})
                raw = urllib.request.urlopen(req, timeout=10).read()
                break
            except Exception:  # noqa: BLE001, S112
                continue
        if not raw:
            return None
        return decode_piece(raw)

    def _solve_once(self, page, cdp) -> tuple[bool, str]:
        import numpy as np
        from PIL import Image

        data = json.loads(self._eval(page, GET_IMGS))
        if data.get("canvas") is None:
            r, _ = self._captcha_result(page)
            return (True, "already_solved") if r == "solved" else (False, "no_captcha")
        if data.get("ok"):
            return True, "already_ok"
        sub, sl, cvr = data.get("sub"), data.get("slider"), data.get("cvr")
        if not sub or not sl or not cvr:
            return False, "layout_missing"
        bg = np.asarray(Image.open(io.BytesIO(base64.b64decode(
            data["canvas"].split(",", 1)[1]))).convert("L")).astype(float)
        H, W = bg.shape
        scale = cvr["w"] / W
        # 优先用拼图 img 自身的矩形；sub 容器可能带 padding/阴影。
        pr = data.get("pr")
        if not pr or pr.get("w", 0) <= 0 or pr.get("h", 0) <= 0:
            pr = sub
        loaded = self._load_piece(page, data)
        if loaded is not None:
            piece, mask, frac = loaded
            # 拼图图片可能带透明留白：用可见内容的占比换算实际尺寸/中心。
            vis_w = pr["w"] * frac[2]
            vis_h = pr["h"] * frac[3]
            vis_cx = pr["x"] + pr["w"] * (frac[0] + frac[2] / 2)
            vis_cy = pr["y"] + pr["h"] * (frac[1] + frac[3] / 2)
            target_w = vis_w / scale
            target_h = vis_h / scale
            expected_y = (vis_cy - cvr["y"]) / (cvr["h"] / H)
            slot = locate_slot(bg, piece, mask, target_w, target_h, expected_y,
                               exclude_cx=(vis_cx - cvr["x"]) / scale,
                               exclude_cy=expected_y)
            piece_cx_css = vis_cx
        else:
            target_w = pr["w"] / scale
            target_h = pr["h"] / scale
            expected_y = (pr["y"] + pr["h"] / 2 - cvr["y"]) / (cvr["h"] / H)
            slot = locate_dark_slot(bg, expected_y)
            piece_cx_css = pr["x"] + pr["w"] / 2
            if slot is not None:
                self._logp("    [solver] 拼图块不可用，使用暗色缺口兜底")
        if slot is None:
            return False, "no_hole"
        hole_cx_css = cvr["x"] + slot.cx * scale
        dx = hole_cx_css - piece_cx_css
        self._logp(f"    [solver] {slot.source} dx={dx:.0f}px "
                   f"score={slot.score:.2f} size={slot.width:.0f}x{slot.height:.0f}")
        if dx < -3:
            return False, "negative_offset"
        dx = max(0.0, dx)
        off_x, off_y = self._frame_offset(page)
        hx = sl["x"] + sl["w"] / 2 + off_x
        hy = sl["y"] + sl["h"] / 2 + off_y
        self._drag(cdp, hx, hy, dx)
        time.sleep(1.4)
        r, _ = self._captcha_result(page)
        if r == "detecting":
            deadline = time.time() + 8
            while time.time() < deadline:
                time.sleep(0.5)
                r, _ = self._captcha_result(page)
                if r != "detecting":
                    break
        if r == "solved":
            return True, "solved"
        if r == "failed":
            self._refresh_captcha(page)
            self._toggle_drag_mode()
            return False, "failed_retry"
        time.sleep(1.2)
        r2, _ = self._captcha_result(page)
        if r2 == "solved":
            return True, "solved_late"
        self._refresh_captcha(page)
        self._toggle_drag_mode()
        return False, f"state_{r}"

    def _drag(self, cdp, x: float, y: float, dx: float) -> None:
        points = build_drag_track(dx, rng=random.Random(int(time.time() * 1000)))
        if self._drag_touch:
            cdp.send("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [{"x": x, "y": y, "id": 1,
                                 "radiusX": 1.5, "radiusY": 1.5, "force": 1}],
            })
        else:
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y,
                "button": "left", "buttons": 1, "clickCount": 1,
            })
        time.sleep(0.08)
        pause_at = int(len(points) * 0.62)
        for i, (px, py) in enumerate(points):
            if self._drag_touch:
                cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchMove",
                    "touchPoints": [{"x": x + px, "y": y + py, "id": 1,
                                     "radiusX": 1.5, "radiusY": 1.5, "force": 1}],
                })
            else:
                cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": x + px, "y": y + py,
                    "button": "left", "buttons": 1,
                })
            delay = 0.017 + random.uniform(0, 0.012)
            if i == pause_at:
                delay += 0.05
            time.sleep(delay)
        time.sleep(0.12)
        if self._drag_touch:
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        else:
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x + dx, "y": y,
                "button": "left", "buttons": 0, "clickCount": 1,
            })

    def _toggle_drag_mode(self) -> None:
        self._drag_touch = not self._drag_touch
        self._logp(f"    [solver] 下次改用{'touch' if self._drag_touch else 'mouse'}")

    def _refresh_captcha(self, page) -> None:
        try:
            if self._eval(page, REFRESH_CAPTCHA):
                self._logp("    [solver] 已刷新滑块")
                time.sleep(1.0)
        except Exception:  # noqa: BLE001, S110
            pass

    def _wait_captcha_and_solve(self, page, cdp, max_auto: int = MAX_CAPTCHA_ATTEMPTS) -> str:
        deadline = time.time() + CAPTCHA_WAIT_S
        seen = False
        while time.time() < deadline:
            r, _ = self._captcha_result(page)
            if r == "solved":
                return "already_solved"
            if r in ("present", "failed", "detecting"):
                seen = True
            if r in ("present", "failed"):
                try:
                    if json.loads(self._eval(page, GET_IMGS)).get("canvas"):
                        break
                except Exception:  # noqa: BLE001, S110
                    pass
            if r == "gone" and self._wait_for_sms_sent(page, 0.1):
                return "already_solved"
            time.sleep(0.4)
        else:
            return "captcha_timeout" if seen else "no_captcha"

        for i in range(max_auto):
            with self._lock:
                self._captcha_attempts = i + 1
            ok, why = self._solve_once(page, cdp)
            self._logp(f"    [solver#{i + 1}] {ok} {why}")
            if ok:
                return "solved"
            if why == "no_captcha":
                return "no_captcha"
            if why == "already_solved":
                return "solved"
            # 重试前等组件重新就绪
            ready = False
            for _ in range(12):
                try:
                    d = json.loads(self._eval(page, GET_IMGS))
                except Exception:  # noqa: BLE001
                    d = {}
                if d.get("canvas") and d.get("sub") and d.get("slider"):
                    ready = True
                    break
                r, _ = self._captcha_result(page)
                if r == "solved":
                    return "solved_late"
                time.sleep(0.5)
            if not ready:
                self._refresh_captcha(page)
        return "timeout"
