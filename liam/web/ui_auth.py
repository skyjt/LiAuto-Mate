"""Web UI 访问认证：LIAM_UI_PASSWORD → HMAC 签名 cookie 会话（零新依赖）。

未设置 LIAM_UI_PASSWORD 时仅警告不拦截（本地开发模式）；
局域网部署必须设置（README/.env.example 已标注）。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request
from fastapi.responses import RedirectResponse

COOKIE_NAME = "liam_session"
SESSION_TTL = 30 * 86400   # 30 天


def _secret(settings) -> bytes:
    """会话签名密钥：LIAM_SECRET_KEY 或首次生成存 meta 表。"""
    if settings.secret_key:
        return settings.secret_key.encode()
    from ..core.db import Database   # 延迟导入避免环
    # settings 层面拿 db 不方便——由 app 装配时注入；这里约定 key 生成在 pages 装配处
    return b""


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
