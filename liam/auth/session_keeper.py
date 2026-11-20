"""SessionKeeper：会话看护循环（方案 §5.2）。

状态机：OK → DEGRADED(仅历史通道) → NEED_LOGIN →(重登)→ OK
  · scope token 每 15min 现换（900s 寿命，提前量）
  · bearer 由 401/100105 反应式续期（不盲刷）；refresh 轮换值立即落库
  · cookie 到期看护：T-3d 起 UI 横幅（M4）；到期 → NEED_LOGIN
M0 验收：重启后凭据恢复 + scope token 自动续期 + 状态端点可查。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .li_api import AuthExpiredError, LiApi, LiApiError
from .session_store import SessionStore

log = logging.getLogger(__name__)

# 状态
S_BOOT = "boot"
S_OK = "ok"
S_DEGRADED = "degraded"          # 历史通道可用，实时通道（scope token）不可用
S_NEED_LOGIN = "need_login"

SCOPE_REFRESH_INTERVAL = 15 * 60   # scope token 现换周期（寿命 900s → 提前 1/3）
KEEPER_TICK = 30                    # 看护循环周期（秒）


class SessionKeeper:
    def __init__(self, api: LiApi, store: SessionStore) -> None:
        self.api = api
        self.store = store
        self.state = S_BOOT
        self.state_reason = ""
        self.scope_token = ""
        self.scope_token_at = 0.0
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self._scope_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _set_state(self, state: str, reason: str = "") -> None:
        if state != self.state:
            log.info("会话状态 %s → %s（%s）", self.state, state, reason)
        self.state = state
        self.state_reason = reason

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.state_reason,
            "has_bearer": bool(self.api.bearer),
            "has_refresh": bool(self.api.refresh_token),
            "has_cookies": bool(self.api.cookies),
            "scope_token_age_s": int(time.time() - self.scope_token_at)
            if self.scope_token_at else None,
            "last_error": self.last_error[:200],
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动看护：先恢复凭据 → 主动刷新 bearer → 进入循环。"""
        if self.store.restore_into(self.api):
            log.info("凭据已从 store 恢复（重启恢复 OK）")
        else:
            self._set_state(S_NEED_LOGIN, "无已存凭据")
            return
        try:
            await self.api.refresh_bearer()      # 主动续期（同时拿到轮换值）
            self.store.persist_all(self.api)     # 轮换值立即落库
            self._set_state(S_OK, "启动即续期成功")
        except AuthExpiredError as e:
            self.last_error = str(e)
            self._set_state(S_NEED_LOGIN, f"refresh 失效: {e.reason}")
            return
        except LiApiError as e:
            # 网络/临时错误：不判死，进循环重试（DEGRADED 而非 NEED_LOGIN）
            self.last_error = str(e)
            self._set_state(S_DEGRADED, f"启动续期临时失败: {e}")
        self._task = asyncio.create_task(self._loop(), name="session-keeper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.api.aclose()

    async def recover(self) -> None:
        """网页重登成功后复位：restore 新凭据 → 主动续期 → 重启看护循环。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.scope_token = ""
        self.scope_token_at = 0.0
        self.store.restore_into(self.api)
        try:
            await self.api.refresh_bearer()
            self.store.persist_all(self.api)
            self._set_state(S_OK, "网页重登成功")
            log.info("重登成功，会话已恢复")
        except Exception as e:  # noqa: BLE001
            self._set_state(S_DEGRADED, f"重登后续期失败: {e}")
        self._task = asyncio.create_task(self._loop(), name="session-keeper")

    # ------------------------------------------------------------------
    # scope token（实时通道钥匙，900s 寿命）
    # ------------------------------------------------------------------

    async def get_scope_token(self) -> str:
        """取可用 scope token：缓存 < 10min 直接用，否则现换。"""
        async with self._scope_lock:
            age = time.time() - self.scope_token_at
            if self.scope_token and age < 600:
                return self.scope_token
            self.scope_token = await self.api.exchange_scope_token()
            self.scope_token_at = time.time()
            if self.state == S_DEGRADED:
                self._set_state(S_OK, "scope token 恢复")
            return self.scope_token

    # ------------------------------------------------------------------
    # 看护循环
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """周期看护：scope token 到期前现换 + bearer 反应式续期探测。"""
        last_scope_refresh = 0.0
        while True:
            try:
                now = time.time()
                # ① scope token 定期现换（登录 cookie 健康时）
                if now - last_scope_refresh >= SCOPE_REFRESH_INTERVAL:
                    await self.get_scope_token()
                    last_scope_refresh = time.time()
                # ② 周期性轻探测 bearer：历史通道保活（低频、只读）
                #    M0 用刷新一次即可；M1 起改为「上次调用失败才刷」
                if now - last_scope_refresh >= SCOPE_REFRESH_INTERVAL:
                    pass  # 见 ①，同一窗口只做一次
            except AuthExpiredError as e:
                self.last_error = str(e)
                if e.need_relogin:
                    self._set_state(S_NEED_LOGIN, e.reason)
                    log.warning("需要重新登录（%s）", e.reason)
                    # 不再循环重试 —— 等 login flow 重新 start()
                    return
                self._set_state(S_DEGRADED, e.reason)
            except LiApiError as e:
                self.last_error = str(e)
                # 临时错误：不动状态（OK 也保持），下轮再看
                log.debug("keeper 临时错误（退避）: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                log.exception("keeper 未预期异常")
            await asyncio.sleep(KEEPER_TICK)
