"""页面路由 + JSON API（M4）：6 页 + /api/v1。

渲染策略：Jinja2 SSR + HTMX 局部刷新（概览 30s）；
图表数据走 /api/v1/*.json 端点由 ECharts 拉取。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..core.db import Database
from .ui_auth import COOKIE_NAME, UiAuth

router = APIRouter()

templates = Jinja2Templates(directory=str(
    __import__("pathlib").Path(__file__).parent / "templates"))


def _ps(v_json: str | None) -> Any:
    """state_latest 的 value_json → python 值（可能双重 JSON）。"""
    if v_json is None:
        return None
    v = json.loads(v_json)
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _state(db: Database, vin: str) -> dict[str, Any]:
    """概览数据装配：latest 快照 + 会话状态徽章 + 新鲜度。"""
    rows = db.query_all("SELECT path, value_json, ts_ms, ingested_at_ms "
                        "FROM state_latest WHERE vin=?", (vin,))
    latest = {r["path"]: r for r in rows}
    st = db.query_one(
        "SELECT state, start_ms FROM states WHERE vin=? AND end_ms IS NULL "
        "ORDER BY start_ms DESC LIMIT 1", (vin,))
    def val(p): return _ps(latest[p]["value_json"]) if p in latest else None
    def ts(p): return latest[p]["ts_ms"] if p in latest else None
    loc = val("Vehicle.Location.CurrentLocationInfo") or {}
    soc = val("Vehicle.Powertrain.Battery.ResidueBattery")
    range_elec = val("Vehicle.Cabin.CLTC.PureElecEnduranceMileInd")
    range_fuel = val("Vehicle.Cabin.CLTC.FuelEnduranceMileInd")
    now_ms = int(time.time() * 1000)
    # 数据新鲜度：拿最近一次采集时间
    anyrow = db.query_one(
        "SELECT MAX(ingested_at_ms) m FROM state_latest WHERE vin=?", (vin,))
    return {
        "state": st["state"] if st else "unknown",
        "since_ms": st["start_ms"] if st else None,
        "soc": soc,
        "range_elec": range_elec, "range_fuel": range_fuel,
        "out_temp": val("Vehicle.VehInfo.CarControl.Hvac.DspOtsdTemp"),
        "in_temp": val("Vehicle.Cabin.AC.FrtACIncarTemp"),
        "loc": ({"lat": loc.get("lat"), "lon": loc.get("lon"),
                "spd": loc.get("spd")}
                if isinstance(loc, dict) and loc.get("lat") is not None
                else None),
        "doors": {
            "main_locked": val("Vehicle.Body.DoorLockStatus.MainDoor"),
            "trunk": val("Vehicle.Body.DoorLockStatus.TrunkDoor"),
        },
        "tires": {
            "FL": val("Vehicle.Chassis.Tire.FLTirePressure"),
            "FR": val("Vehicle.Chassis.Tire.FRTirePressure"),
            "RL": val("Vehicle.Chassis.Tire.RLTirePressure"),
            "RR": val("Vehicle.Chassis.Tire.RRTirePressure"),
        },
        "sentry": val("Vehicle.Sentry.SentinelStatus"),
        "charge_status": val("Vehicle.Powertrain.Battery.ChargeStatus"),
        "online": val("Vehicle.ConnectManager.ConnectStatus.5G"),
        "last_ingest_ms": anyrow["m"] if anyrow and anyrow["m"] else None,
        "now_ms": now_ms,
    }


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------

async def _page(request: Request, tmpl: str, **ctx) -> HTMLResponse:
    auth: UiAuth = request.app.state.ui_auth
    if not auth.guard(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, tmpl, ctx)


@router.get("/", response_class=HTMLResponse)
async def page_overview(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    keeper = request.app.state.keeper_ref.get("keeper")
    return await _page(
        request, "overview.html",
        v=_state(db, vin), session=keeper.status() if keeper else None,
        refresh=30)


@router.get("/drives", response_class=HTMLResponse)
async def page_drives(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    sessions = db.query_all(
        "SELECT * FROM drive_sessions WHERE vin=? ORDER BY start_ms DESC "
        "LIMIT 100", (vin,))
    return await _page(request, "drives.html", sessions=sessions)


@router.get("/charging", response_class=HTMLResponse)
async def page_charging(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    sessions = db.query_all(
        "SELECT * FROM charge_sessions WHERE vin=? ORDER BY start_ms DESC "
        "LIMIT 100", (vin,))
    cloud = db.query_all(
        "SELECT * FROM charge_records_cloud WHERE vin=? "
        "ORDER BY start_ms DESC LIMIT 100", (vin,))
    return await _page(request, "charging.html", sessions=sessions, cloud=cloud)


@router.get("/trends", response_class=HTMLResponse)
async def page_trends(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    paths = [r["path"] for r in db.query_all(
        "SELECT DISTINCT path FROM state_latest WHERE vin=? ORDER BY path",
        (vin,))]
    return await _page(request, "trends.html", paths=paths)


@router.get("/battery", response_class=HTMLResponse)
async def page_battery(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    months = db.query_all(
        "SELECT date, mileage_km, energy_kwh FROM travel_daily "
        "WHERE vin=? AND date LIKE '20%' ORDER BY date", (vin,))
    return await _page(request, "battery.html", months=months)


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    keeper = request.app.state.keeper_ref.get("keeper")
    n_latest = db.query_one("SELECT COUNT(*) c FROM state_latest")["c"]
    n_hist = db.query_one("SELECT COUNT(*) c FROM state_history")["c"]
    return await _page(
        request, "settings.html",
        s=settings, keeper=keeper.status() if keeper else None,
        n_latest=n_latest, n_hist=n_hist)


@router.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    auth: UiAuth = request.app.state.ui_auth
    if auth.guard(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"err": ""})


@router.post("/login")
async def do_login(request: Request):
    auth: UiAuth = request.app.state.ui_auth
    form = await request.form()
    password = str(form.get("password") or "")
    if auth.check_password(password):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(COOKIE_NAME, auth.make_cookie(),
                        max_age=30 * 86400, httponly=True, samesite="lax")
        return resp
    return templates.TemplateResponse(
        request, "login.html", {"err": "密码不正确"}, status_code=401)


# ----------------------------------------------------------------------
# JSON API（UI 图表数据 + 用户脚本）
# ----------------------------------------------------------------------

@router.get("/api/v1/overview.json")
async def api_overview(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    return _state(db, vin)


@router.get("/api/v1/signal/{path:path}.json")
async def api_signal(request: Request, path: str, hours: int = 24,
                     limit: int = 2000):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    since = int(time.time() * 1000) - hours * 3600 * 1000
    rows = db.query_all(
        "SELECT ts_ms, ingested_at_ms, value_json FROM state_history "
        "WHERE vin=? AND path=? AND ingested_at_ms>? "
        "ORDER BY ingested_at_ms LIMIT ?",
        (vin, path, since, limit))
    out = []
    for r in rows:
        v = _ps(r["value_json"])
        if isinstance(v, dict):        # 数值化：图表要标量
            v = v.get("spd") or v.get("value") or None
        out.append({"t": r["ingested_at_ms"], "v": v})
    return JSONResponse({"path": path, "points": out})


@router.get("/api/v1/positions.json")
async def api_positions(request: Request, session_id: int):
    db: Database = request.app.state.db
    rows = db.query_all(
        "SELECT ts_ms, lat, lon, spd, dir FROM positions "
        "WHERE drive_session_id=? ORDER BY ts_ms", (session_id,))
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/v1/charge-samples.json")
async def api_charge_samples(request: Request, session_id: int):
    db: Database = request.app.state.db
    rows = db.query_all(
        "SELECT ts_ms, soc, power_kw, current, voltage FROM charge_samples "
        "WHERE charge_session_id=? ORDER BY ts_ms", (session_id,))
    return JSONResponse([dict(r) for r in rows])
