"""FastAPI 应用装配：M0 healthz/status + M4 页面/静态/UI 认证 + MCP。"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from ..service import service_lifespan

log = logging.getLogger(__name__)


def _ctime(ts_ms: float | None) -> str:
    """模板过滤器：epoch 毫秒 → 本地时间串（直接吃 ms，模板勿再除 1000——
    Jinja 中 `x / 1000 | ctime` 会解析成 `x / (1000|ctime)`）。"""
    try:
        if ts_ms is None:
            return "—"
        return datetime.datetime.fromtimestamp(float(ts_ms) / 1000).strftime(
            "%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def create_app(settings: Settings, keeper_ref: dict,
               lifespan=None, db=None, vin: str | None = None) -> FastAPI:
    """装配 FastAPI app。

    keeper_ref 接受运行中的会话看护。VIN 默认读取数据库中的绑定记录。
    """
    app = FastAPI(
        title="LiAuto-Mate",
        version=__version__,
        docs_url=None, redoc_url=None,
        lifespan=lifespan or service_lifespan,
    )
    app.state.settings = settings
    app.state.keeper_ref = keeper_ref
    app.state.db = db
    if vin is None and db is not None:
        stored = db.query_one("SELECT value FROM meta WHERE key='vin'")
        vin = stored["value"] if stored else ""
    app.state.vin = vin or ""
    app.state.collection_changed = asyncio.Event()

    # UI 认证（密码未设=开发模式放行）
    from .ui_auth import UiAuth
    secret = (settings.secret_key or _load_or_create_secret(db)).encode()
    app.state.ui_auth = UiAuth(settings.ui_password, secret)

    # 静态文件 + 页面（static 在包根 liam/static，非 web/ 子目录）
    static = Path(__file__).parent.parent / "static"
    app.mount("/static", StaticFiles(directory=str(static)), name="static")
    from .pages import password_router, templates
    from .pages import router as pages_router
    templates.env.filters["ctime"] = _ctime
    app.include_router(pages_router)
    app.include_router(password_router)

    # 网页重登面板（M5/D4）：flow 需要 SessionStore（凭据落库）
    from ..auth.session_store import SessionStore
    from ..core.secrets import SecretBox
    from ..login.flow import LoginFlow
    from .login_routes import router as login_router
    app.state.login_flow = LoginFlow(
        SessionStore(db, SecretBox.load_or_create(settings.data_dir)),
        headless=settings.login_headless)
    app.include_router(login_router)

    # MCP（token 未设=整体关闭，方案 §8.3）
    if settings.mcp_token:
        try:
            from ..mcp_server import mount_mcp
            mount_mcp(app, db, keeper_ref, settings)
            log.info("MCP 已挂载 /mcp（Bearer 必需）")
        except Exception as e:  # noqa: BLE001
            log.error("MCP 挂载失败（继续无 MCP 运行）: %r", e)
    else:
        log.info("LIAM_MCP_TOKEN 未设置：MCP 关闭")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/v1/system/status")
    async def system_status() -> dict[str, Any]:
        keeper = keeper_ref.get("keeper")
        session = keeper.status() if keeper else {"state": "not_started"}
        return {"service": "liauto-mate", "version": __version__,
                "session": session}

    return app


def _load_or_create_secret(db) -> str:
    """UI 会话签名密钥：meta 表持久化（重启不丢登录态）。"""
    if db is None:
        return secrets.token_hex(16)
    row = db.query_one("SELECT value FROM meta WHERE key='ui_secret'")
    if row and row["value"]:
        return row["value"]
    val = secrets.token_hex(32)
    db.execute("INSERT INTO meta(key, value) VALUES('ui_secret', ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (val,))
    return val
