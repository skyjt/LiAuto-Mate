"""从正常 ASGI 生命周期验证首次接车、会话失效和热恢复，全程使用本地替身。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from liam.auth.li_api import AuthExpiredError
from liam.auth.session_keeper import S_DEGRADED, S_NEED_LOGIN, S_OK, SessionKeeper
from liam.auth.session_store import K_BEARER, K_REFRESH, SessionStore
from liam.collector.cloud_sync import CloudSync
from liam.collector.collector import Collector
from liam.config import Settings
from liam.core.db import Database
from liam.core.secrets import SecretBox
from liam.login.flow import S_DONE, S_FAILED
from liam.web.app import create_app

VIN = "TESTCAR0000000001"


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "test.db")
    store = SessionStore(db, SecretBox.load_or_create(tmp_path))
    api = SimpleNamespace(
        vin="", bearer="", refresh_token="", cookies={}, device_id="fixture",
        refresh_bearer=AsyncMock(), exchange_scope_token=AsyncMock(return_value="fixture-scope"),
        aclose=AsyncMock())
    keeper = SessionKeeper(api, store)
    settings = Settings(_env_file=None, data_dir=tmp_path, ui_password="", mcp_token="")
    try:
        yield create_app(settings, {"keeper": keeper}, db=db), keeper, store
    finally:
        db.close()


async def until(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("bind_first", [True, False])
async def test_bind_and_recover_once_in_either_order(service, monkeypatch, bind_first):
    app, keeper, store = service
    collector_runs, sync_runs, stopped = [], [], []
    errors = asyncio.Queue()

    async def collect(worker):
        collector_runs.append(worker)
        try:
            raise await errors.get()
        finally:
            stopped.append("collector")

    async def sync(worker):
        sync_runs.append(worker)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("cloud-sync")

    monkeypatch.setattr(Collector, "run_forever", collect)
    monkeypatch.setattr(CloudSync, "run_daily", sync)
    flow = app.state.login_flow
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert keeper.state == S_NEED_LOGIN
        if bind_first:
            assert (await client.post("/settings/vehicle", data={"vin": VIN})).status_code == 303
            assert not collector_runs and not sync_runs

        store.save_login("fixture-bearer", "fixture-refresh", {}, "fixture")
        await asyncio.to_thread(flow.credentials_saved)
        # 即使浏览器没有轮询 finish，服务也应自行恢复。
        await until(lambda: flow.status()["state"] == S_DONE)
        assert keeper.state == S_OK
        result = (await client.get("/liam-login/finish.json")).json()
        assert result == {"ok": True, "state": S_DONE, "next": "/" if bind_first else "/settings#session"}
        if not bind_first:
            assert not collector_runs and not sync_runs
        await client.post("/settings/vehicle", data={"vin": VIN.lower()})
        await until(lambda: len(collector_runs) == len(sync_runs) == 1)
        assert keeper.api.vin == collector_runs[0].vin == sync_runs[0].vin == VIN
        await client.get("/liam-login/finish.json")
        keeper.api.refresh_bearer.assert_awaited_once()

        # 实时票据到期时，历史任务继续使用同一次运行。
        await errors.put(AuthExpiredError("fixture-scope"))
        await until(lambda: keeper.state == S_DEGRADED)
        await keeper.get_scope_token()
        await until(lambda: len(collector_runs) == 2)
        assert collector_runs[0] is collector_runs[1]
        assert len(sync_runs) == 1

        # 账号失效后两个任务都停，重登后各补起一个。
        await errors.put(AuthExpiredError("fixture-login", need_relogin=True))
        await until(lambda: keeper.state == S_NEED_LOGIN and "cloud-sync" in stopped)
        await asyncio.to_thread(flow.credentials_saved)
        await until(lambda: len(collector_runs) == 3 and len(sync_runs) == 2)
        assert keeper.api.refresh_bearer.await_count == 2

    assert stopped.count("collector") == 3 and stopped.count("cloud-sync") == 2
    keeper.api.aclose.assert_awaited_once()


async def test_invalid_saved_session_does_not_report_login_success(service):
    app, keeper, store = service
    async with app.router.lifespan_context(app):
        store.save(K_REFRESH, "fixture-expired")
        keeper.api.refresh_bearer.side_effect = AuthExpiredError("fixture-invalid", need_relogin=True)
        app.state.login_flow.credentials_saved()
        await until(lambda: app.state.login_flow.status()["state"] == S_FAILED)
        assert keeper.state == S_NEED_LOGIN
        keeper.api.exchange_scope_token.assert_not_awaited()


async def test_restart_with_saved_vin_and_bearer(service, monkeypatch):
    app, keeper, store = service
    store.save(K_REFRESH, "previous-login")
    store.save_login("fixture-saved", None, {}, "fixture")
    assert store.load(K_BEARER) == "fixture-saved" and not store.load(K_REFRESH)
    stored_vin = VIN.lower()
    app.state.db.execute("INSERT INTO meta VALUES('vin',?)", (stored_vin,))
    restarted = create_app(app.state.settings, {"keeper": keeper}, db=app.state.db)
    started = []

    async def run(worker):
        started.append((type(worker).__name__, worker.vin))
        await asyncio.Event().wait()

    monkeypatch.setattr(Collector, "run_forever", run)
    monkeypatch.setattr(CloudSync, "run_daily", run)
    async with restarted.router.lifespan_context(restarted):
        await until(lambda: len(started) == 2)
        assert set(started) == {("Collector", stored_vin), ("CloudSync", stored_vin)}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client:
            result = await client.post("/settings/vehicle", data={"vin": VIN})
            assert result.headers["location"] == "/settings#session"
        assert keeper.state == S_OK
        keeper.api.refresh_bearer.assert_not_awaited()
        keeper.api.exchange_scope_token.assert_awaited_once()
