"""FastAPI 应用：M0 阶段 = /healthz + /api/v1/system/status。

Web UI（M4）/ MCP（M4）都挂同一 app；此处先立骨架与装配点。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from . import __version__
from .config import Settings

log = logging.getLogger(__name__)


def create_app(settings: Settings, keeper_ref: dict,
               lifespan=None) -> FastAPI:
    """装配 FastAPI app。

    keeper_ref: {"keeper": SessionKeeper|None} —— 由 serve 注入；
    lifespan: 由 serve 提供（负责 keeper start/stop）。
    """
    app = FastAPI(
        title="LiAuto-Mate",
        version=__version__,
        docs_url=None, redoc_url=None,   # 单用户自用，不暴露文档面
        lifespan=lifespan,
    )

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
