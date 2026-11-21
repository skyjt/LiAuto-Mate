"""用临时 SQLite 和本地 ASGI 请求验证看板回归。"""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from liam.collector.cloud_sync import CloudSync
from liam.config import Settings
from liam.core.db import Database
from liam.web.app import create_app


@pytest.fixture
async def ui(tmp_path):
    db = Database(tmp_path / "liam.db")
    settings = Settings(_env_file=None, ui_password="fixture-password", data_dir=tmp_path)
    app = create_app(settings, {}, db=db, vin="TEST")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, db
    finally:
        db.close()


async def sign_in(client, target="/"):
    response = await client.post("/login", data={"password": "fixture-password", "next": target})
    assert response.status_code == 303
    assert response.headers["location"] == target


async def test_ui_session_and_return_page(ui):
    client, _ = ui
    entry = await client.get("/trends")
    assert entry.headers["location"] == "/login?next=/trends"
    for path in ("/api/v1/overview.json", "/liam-login/status.json"):
        assert (await client.get(path)).status_code == 401
    for method, path in (("GET", "/settings/export"), ("POST", "/settings/geofence/delete")):
        assert (await client.request(method, path)).status_code == 303
    await sign_in(client, "/trends")
    assert "暂无可选信号" in (await client.get("/trends")).text
    assert (await client.get("/api/v1/overview.json")).json()["doors"]["main_locked"] is None


async def test_signal_keeps_newest_points_in_order_and_zero_speed(ui):
    client, db = ui
    await sign_in(client)
    now = int(time.time() * 1000)
    for offset, speed in enumerate((15, 3, 0)):
        db.execute(
            "INSERT INTO state_history(vin,path,ingested_at_ms,value_json) VALUES(?,?,?,?)",
            ("TEST", "test.location", now + offset, json.dumps({"spd": speed})),
        )
    data = (await client.get("/api/v1/signal/test.location.json?limit=2")).json()
    assert data["truncated"]
    assert data["points"] == [{"t": now + 1, "v": 3}, {"t": now + 2, "v": 0}]


async def test_soc_buckets_only_count_valid_samples(ui):
    client, db = ui
    await sign_in(client)
    for value in (-2, 0, 99.5, 100, 105, "unavailable"):
        db.execute(
            "INSERT INTO state_history(vin,path,ingested_at_ms,value_json) VALUES(?,?,?,?)",
            ("TEST", "Vehicle.Powertrain.Battery.ResidueBattery",
             int(time.time() * 1000), json.dumps(value)),
        )
    data = (await client.get("/api/v1/soc-histogram.json")).json()
    assert data["samples"] == 3
    assert data["buckets"] == [1] + [0] * 8 + [2]


async def test_settings_export_includes_committed_wal_data(ui, tmp_path):
    client, db = ui
    await sign_in(client)
    fields = {"name": "测试地点", "lat": "0", "lon": "0", "radius": "0"}
    result = await client.post("/settings/geofence/add", data=fields, follow_redirects=True)
    assert 'role="alert"' in result.text
    assert not db.query_all("SELECT id FROM geofences")
    await client.post("/settings/geofence/add", data={**fields, "radius": "100"})
    response = await client.get("/settings/export?table=db")
    assert response.status_code == 200
    export = tmp_path / "download.db"
    export.write_bytes(response.content)
    with closing(sqlite3.connect(export)) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert snapshot.execute("SELECT name FROM geofences").fetchone() == ("测试地点",)


async def test_first_binding_is_persistent_and_same_vehicle_retries_are_safe(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, ui_password="fixture-password")
    with closing(Database(tmp_path / "liam.db")) as db:
        app = create_app(settings, {}, db=db)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            url = "/settings/vehicle"
            vins = ("TESTCAR0000000001", "TESTCAR0000000002")
            assert (await client.post(url, data={"vin": vins[0]})).headers["location"] == "/login"
            await sign_in(client)
            for invalid in ("", "123", "TESTCAR000000000I", "TESTCAR000000000Ｏ"):
                result = await client.post(url, data={"vin": invalid}, follow_redirects=True)
                assert 'role="alert"' in result.text
            assert db.query_one("SELECT value FROM meta WHERE key='vin'") is None
            responses = await asyncio.gather(*(client.post(url, data={"vin": v.lower()}) for v in vins))
            assert sum(r.headers["location"] == "/settings#session" for r in responses) == 1
            bound = app.state.vin
            assert bound in vins
            assert (await client.post(url, data={"vin": bound})).headers["location"] == "/settings#session"
            page = (await client.get("/settings")).text
            assert f"VIN 尾号 {bound[-4:]}" in page and 'name="vin"' not in page
        assert create_app(settings, {}, db=db).state.vin == bound


async def test_detail_queries_and_csv_use_the_bound_vehicle(ui):
    client, db = ui
    await sign_in(client)
    for vin in ("TEST", "OTHER"):
        trip = db.insert(
            "INSERT INTO drive_sessions(vin,start_ms,created_at_ms,updated_at_ms) VALUES(?,1,1,1)",
            (vin,))
        db.execute("INSERT INTO positions(vin,drive_session_id,ts_ms,lat,lon) VALUES(?,?,1,0,0)",
                   (vin, trip))
        charge = db.insert(
            "INSERT INTO charge_sessions(vin,start_ms,created_at_ms,updated_at_ms) VALUES(?,1,1,1)",
            (vin,))
        db.execute("INSERT INTO charge_samples(vin,charge_session_id,ts_ms,soc) VALUES(?,?,1,0)",
                   (vin, charge))
        for path, session_id in (("positions", trip), ("charge-samples", charge)):
            result = await client.get(f"/api/v1/{path}.json", params={"session_id": session_id})
            assert len(result.json()) == (1 if vin == "TEST" else 0)
    for table in ("sessions", "charge"):
        csv = (await client.get("/settings/export", params={"table": table})).text
        assert "TEST" in csv and "OTHER" not in csv


async def test_login_panel_cooldown_only_after_sms_and_phone_is_editable(tmp_path):
    from liam.login.flow import S_FAILED

    settings = Settings(_env_file=None, data_dir=tmp_path, ui_password="fixture-password")
    with closing(Database(tmp_path / "liam.db")) as db:
        app = create_app(settings, {}, db=db)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await sign_in(client)
            page = (await client.get("/liam-login")).text
            assert "自动完成滑块验证" in page
            assert "readonly" not in page

            flow = app.state.login_flow
            flow._mark_sms_sent()
            blocked = await client.post(
                "/liam-login/start", data={"phone": "13800138000"})
            assert blocked.status_code == 429
            assert blocked.json()["cooldown_left"] > 0

            # 滑块失败发生在短信发出前：不应留下冷却，用户可以立即重试。
            flow._sms_sent_at = 0.0
            flow._set(S_FAILED, "滑块未通过（timeout）")
            status = (await client.get("/liam-login/status.json")).json()
            assert status["state"] == S_FAILED
            assert status["sms_sent"] is False
            assert status["cooldown_left"] == 0

            flow.dependency_problem = lambda: "缺少登录组件（测试）"
            page = (await client.get("/liam-login")).text
            assert "缺少登录组件（测试）" in page and "disabled" in page


async def test_cloud_charge_corrections_reach_monthly_chart(ui):
    client, db = ui
    await sign_in(client)
    row = {"id": "fixture-record", "startTimeMs": 1795000000000, "capacity": 0}
    records = AsyncMock(side_effect=[[row], [{**row, "chargingCapacity": 12.5}], [{"id": row["id"]}]])
    sync = CloudSync(SimpleNamespace(get_charge_records=records), db, "TEST")
    for expected in (0, 12.5, 12.5):
        await sync._sync_charge_records()
        months = (await client.get("/api/v1/charge-monthly.json")).json()
        assert len(months) == 1 and months[0]["n"] == 1
        assert months[0]["kwh"] == expected


async def test_mcp_session_sees_binding_and_matches_dashboard(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, ui_password="", mcp_token="fixture-token")
    with closing(Database(tmp_path / "liam.db")) as db:
        app = create_app(settings, {}, db=db)
        vin = "TESTCAR0000000001"
        db.execute(
            "INSERT INTO state_latest(vin,path,value_json,ingested_at_ms) VALUES(?,?,?,?)",
            (vin, "Vehicle.Body.DoorLockStatus.MainDoor", '{"value":"0"}', 1))
        for index, value in enumerate((0, 100, -2, 105)):
            db.execute(
                "INSERT INTO state_history(vin,path,value_json,ingested_at_ms) VALUES(?,?,?,?)",
                (vin, "Vehicle.Powertrain.Battery.ResidueBattery", str(value),
                 int(time.time() * 1000) + index))

        async with (
            create_connected_server_and_client_session(app.state.mcp) as session,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            assert (await client.post("/mcp/")).status_code == 401
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert len(names) == 15 and all(name.startswith("get_") for name in names)

            async def query(name):
                result = await session.call_tool(name, {})
                assert not result.isError
                return json.loads(result.content[0].text)

            assert (await query("get_vehicle_status"))["doors_locked"] is None
            await client.post("/settings/vehicle", data={"vin": vin})
            assert (await query("get_vehicle_status"))["doors_locked"] is False
            health = await query("get_battery_health")
            chart = (await client.get("/api/v1/soc-histogram.json")).json()
            assert chart["samples"] == health["soc_samples_30d"] == 2
            assert health["soc_distribution"]["80-100"] == 1
