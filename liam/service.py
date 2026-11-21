"""服务内的采集任务随 VIN 和账号会话的变化启停。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from .auth.li_api import AuthExpiredError
from .auth.session_keeper import S_DEGRADED, S_NEED_LOGIN, S_OK, SessionKeeper
from .collector.cloud_sync import CloudSync
from .collector.collector import Collector
from .login.flow import S_DONE, S_FAILED, S_RECOVERING

log = logging.getLogger(__name__)


async def _cancel(tasks) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _worker(name: str, run: Callable[[], Awaitable[None]], keeper: SessionKeeper) -> None:
    while True:
        try:
            await run()
            return
        except AuthExpiredError as exc:
            keeper.report_auth_error(exc)
            return
        except Exception:
            log.exception("%s 中断，120 秒后重试", name)
            await asyncio.sleep(120)


async def _manage_collection(state, keeper: SessionKeeper) -> None:
    jobs = {}
    collector = None
    changed = state.collection_changed
    flow = state.login_flow
    try:
        while True:
            changed.clear()
            if flow.status()["state"] == S_RECOVERING:
                # 先停掉使用旧凭据的请求，再恢复新会话。
                await _cancel(jobs.values())
                jobs.clear()
                try:
                    await keeper.recover()
                except Exception:
                    log.exception("无法载入已保存的账号会话")
                    flow._set(S_FAILED, "会话恢复失败，请查看服务日志。")
                else:
                    if keeper.state == S_NEED_LOGIN:
                        flow._set(S_FAILED, "账号凭据已失效，请重新登录。")
                    else:
                        flow._set(S_DONE)

            vin = state.vin
            realtime = bool(vin) and keeper.state == S_OK
            history = bool(vin) and keeper.state in (S_OK, S_DEGRADED)
            for name, enabled in (("collector", realtime), ("cloud-sync", history)):
                if not enabled and name in jobs:
                    await _cancel([jobs.pop(name)])
            if changed.is_set():
                continue
            if history:
                keeper.api.vin = vin
            if realtime and ("collector" not in jobs or jobs["collector"].done()):
                if collector is None:
                    collector = Collector(keeper.api, keeper, state.db, state.settings, vin)
                jobs["collector"] = asyncio.create_task(
                    _worker("collector", collector.run_forever, keeper), name="collector")
            if history and ("cloud-sync" not in jobs or jobs["cloud-sync"].done()):
                sync = CloudSync(keeper.api, state.db, vin, state.settings.retention_days)
                jobs["cloud-sync"] = asyncio.create_task(
                    _worker("cloud-sync", sync.run_daily, keeper), name="cloud-sync")
            await changed.wait()
    finally:
        await _cancel(jobs.values())


@asynccontextmanager
async def service_lifespan(app):
    state = app.state
    keeper = state.keeper_ref.get("keeper")
    async with AsyncExitStack() as stack:
        if keeper is not None:
            stack.push_async_callback(keeper.stop)
            await keeper.start()
            changed = state.collection_changed
            loop = asyncio.get_running_loop()
            keeper.on_state_change = changed.set
            state.login_flow.on_saved = lambda: loop.call_soon_threadsafe(changed.set)
            supervisor = asyncio.create_task(_manage_collection(state, keeper), name="collection")
            stack.push_async_callback(_cancel, [supervisor])
            stack.callback(setattr, state.login_flow, "on_saved", None)
            stack.callback(setattr, keeper, "on_state_change", None)
        if hasattr(state, "mcp"):
            await stack.enter_async_context(state.mcp.session_manager.run())
        yield
