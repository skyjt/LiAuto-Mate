"""Web UI 访问认证：LIAM_UI_PASSWORD → HMAC 签名 cookie 会话（零新依赖）。

未设置 LIAM_UI_PASSWORD 时仅警告不拦截（本地开发模式）；
局域网部署必须设置（README/.env.example 已标注）。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

COOKIE_NAME = "liam_session"
SESSION_TTL = 30 * 86400   # 30 天


UI_PAGES = {"/", "/drives", "/charging", "/trends", "/battery", "/settings", "/liam-login"}


async def require_ui_session(request: Request) -> None:
    if request.app.state.ui_auth.guard(request):
        return
    path = request.url.path
    if path.endswith(".json") or path.startswith("/liam-login/"):
        raise HTTPException(401, "请先登录看板")
    target = f"/login?next={path}" if path in UI_PAGES else "/login"
    raise HTTPException(303, headers={"Location": target})


class UiAuth:
    def __init__(self, password: str, secret: bytes) -> None:
        self.password_hash = (hashlib.sha256(password.encode()).hexdigest()
                              if password else "")
        self.secret = secret or b"liam-dev-secret"

    @property
    def enabled(self) -> bool:
        return bool(self.password_hash)

    def _sign(self, ts: int) -> str:
        return hmac.new(self.secret, str(ts).encode(), hashlib.sha256).hexdigest()

    def make_cookie(self) -> str:
        ts = int(time.time())
        return f"{ts}.{self._sign(ts)}"

    def check_cookie(self, value: str | None) -> bool:
        if not self.enabled:
            return True    # 未设密码=开发模式放行
        if not value or "." not in value:
            return False
        ts_s, sig = value.split(".", 1)
        try:
            ts = int(ts_s)
        except ValueError:
            return False
        if time.time() - ts > SESSION_TTL:
            return False
        return hmac.compare_digest(self._sign(ts), sig)

    def check_password(self, password: str) -> bool:
        return hmac.compare_digest(
            hashlib.sha256(password.encode()).hexdigest(), self.password_hash)

    def guard(self, request: Request) -> bool:
        return self.check_cookie(request.cookies.get(COOKIE_NAME))
