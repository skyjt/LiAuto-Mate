"""页面路由 + JSON API（M4）：6 页 + /api/v1。

渲染策略：Jinja2 SSR + JSON 局部刷新（概览 30s）；
图表数据走 /api/v1/*.json 端点由 ECharts 拉取。
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from ..collector.analysis import _ps, soc_buckets, switch_value
from ..config import Settings
from ..core.db import Database
from .ui_auth import COOKIE_NAME, UI_PAGES, UiAuth, require_ui_session

router = APIRouter(dependencies=[Depends(require_ui_session)])
password_router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _state(db: Database, vin: str) -> dict[str, Any]:
    """概览数据装配：latest 快照 + 会话状态徽章 + 新鲜度。"""
    rows = db.query_all("SELECT path, value_json, ts_ms, ingested_at_ms "
                        "FROM state_latest WHERE vin=?", (vin,))
    latest = {r["path"]: r for r in rows}
    st = db.query_one(
        "SELECT state, start_ms FROM states WHERE vin=? AND end_ms IS NULL "
        "ORDER BY start_ms DESC LIMIT 1", (vin,))
    def val(path):
        return _ps(latest[path]["value_json"]) if path in latest else None

    def switch(path):
        return switch_value(val(path))

    loc = val("Vehicle.Location.CurrentLocationInfo") or {}
    soc = val("Vehicle.Powertrain.Battery.ResidueBattery")
    range_elec = val("Vehicle.Cabin.CLTC.PureElecEnduranceMileInd")
    range_fuel = val("Vehicle.Cabin.CLTC.FuelEnduranceMileInd")
    # 车外温度十分度编码（实测 164.0 = 16.4℃ —— App 端 ÷10 显示）
    out_raw = val("Vehicle.VehInfo.CarControl.Hvac.DspOtsdTemp")
    try:
        out_temp = round(float(out_raw) / 10, 1) if out_raw is not None else None
    except (TypeError, ValueError):
        out_temp = None
    now_ms = int(time.time() * 1000)
    # 数据新鲜度：拿最近一次采集时间
    anyrow = db.query_one(
        "SELECT MAX(ingested_at_ms) m FROM state_latest WHERE vin=?", (vin,))
    return {
        "state": st["state"] if st else "unknown",
        "since_ms": st["start_ms"] if st else None,
        "soc": soc,
        "range_elec": range_elec, "range_fuel": range_fuel,
        "out_temp": out_temp,
        "in_temp": val("Vehicle.Cabin.AC.FrtACIncarTemp"),
        "loc": ({"lat": loc.get("lat"), "lon": loc.get("lon"),
                "spd": loc.get("spd")}
                if isinstance(loc, dict) and loc.get("lat") is not None
                else None),
        "doors": {
            "main_locked": switch("Vehicle.Body.DoorLockStatus.MainDoor"),
            "trunk": switch("Vehicle.Body.DoorLockStatus.TrunkDoor"),
            # 门开关（实测 11-20 静止全 0 → 0=关，非零=开；尾门枚举语义
            # 未标定不单独高亮；充电口盖同理）
            "door_main": switch("Vehicle.Body.DoorSwitchStatus.MainDoor"),
            "door_copilot": switch("Vehicle.Body.DoorSwitchStatus.CopilotDoor"),
            "door_bl": switch("Vehicle.Body.DoorSwitchStatus.BackLeftDoor"),
            "door_br": switch("Vehicle.Body.DoorSwitchStatus.BackRightDoor"),
        },
        "charge_power": val("Vehicle.Powertrain.Battery.CLTCChargePower"),
        "charge_left_min": val(
            "Vehicle.Powertrain.Battery.ChargeSurplusTime"),
        "tires": {
            "FL": val("Vehicle.Chassis.Tire.FLTirePressure"),
            "FR": val("Vehicle.Chassis.Tire.FRTirePressure"),
            "RL": val("Vehicle.Chassis.Tire.RLTirePressure"),
            "RR": val("Vehicle.Chassis.Tire.RRTirePressure"),
        },
        "sentry": switch("Vehicle.Sentry.SentinelStatus"),
        "charge_status": val("Vehicle.Powertrain.Battery.ChargeStatus"),
        "online": switch("Vehicle.ConnectManager.ConnectStatus.5G"),
        "last_ingest_ms": anyrow["m"] if anyrow and anyrow["m"] else None,
        "now_ms": now_ms,
    }


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------

def _page(request: Request, tmpl: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, tmpl, ctx)


@router.get("/", response_class=HTMLResponse)
async def page_overview(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    keeper = request.app.state.keeper_ref.get("keeper")
    return _page(
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
    return _page(request, "drives.html", sessions=sessions)


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
    return _page(request, "charging.html", sessions=sessions, cloud=cloud)


@router.get("/trends", response_class=HTMLResponse)
async def page_trends(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    rows = db.query_all(
        "SELECT DISTINCT path FROM state_latest WHERE vin=? ORDER BY path",
        (vin,))
    # 按二级域分组（optgroup）
    groups: dict[str, list[str]] = {}
    for r in rows:
        parts = r["path"].split(".")
        key = ".".join(parts[:3]) if len(parts) > 3 else ".".join(parts[:2])
        groups.setdefault(key, []).append(r["path"])
    return _page(request, "trends.html", groups=groups)


@router.get("/battery", response_class=HTMLResponse)
async def page_battery(request: Request):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    months = db.query_all(
        "SELECT date, mileage_km, energy_kwh FROM travel_daily "
        "WHERE vin=? AND date LIKE '20%' ORDER BY date", (vin,))
    return _page(request, "battery.html", months=months)


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request, name: str = "", lat: str = "",
                        lon: str = ""):
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    keeper = request.app.state.keeper_ref.get("keeper")
    vin = request.app.state.vin
    n_latest = db.query_one("SELECT COUNT(*) c FROM state_latest WHERE vin=?", (vin,))["c"]
    n_hist = db.query_one("SELECT COUNT(*) c FROM state_history WHERE vin=?", (vin,))["c"]
    geofences = db.query_all("SELECT * FROM geofences ORDER BY name")
    ota_rows = db.query_all(
        "SELECT path, value_json FROM state_latest "
        "WHERE vin=? AND (path LIKE 'Vehicle.OTA.%' "
        "OR path LIKE 'Vehicle.Version.OTA%') ", (vin,))
    maint_rows = db.query_all(
        "SELECT path, value_json, ts_ms FROM state_latest "
        "WHERE vin=? AND path LIKE 'Vehicle.Carcenter.Maintain.%' "
        "AND path != 'Vehicle.Carcenter.Maintain.Config'",
        (vin,))
    ota = {r["path"]: _ps(r["value_json"]) for r in ota_rows}
    # 当前版本号嵌在 UpgradeProgress.currentVersion
    prog = ota.get("Vehicle.OTA.Upgrade.UpgradeProgress")
    ota_version = (prog.get("currentVersion") if isinstance(prog, dict)
                   else None)
    # 保养项人性化：提取 名称/剩余里程/到期日（langName 是内嵌 JSON）
    maint = []
    for r in maint_rows:
        v = _ps(r["value_json"])
        item = {"item": r["path"].split(".")[-1], "value": v}
        if isinstance(v, dict):
            # ★ 勿用 `name` 作局部变量——会遮蔽函数参数 name（query 预填）
            item_name = v.get("name")
            if isinstance(item_name, str) and item_name[:1] == "{":
                try:
                    item_name = json.loads(item_name).get("CN", item_name)
                except ValueError:
                    pass
            left = v.get("maintainLeftMileage")
            due = v.get("maintainDueDate")
            due_s = (f"{due[:4]}-{due[4:6]}-{due[6:]}"
                     if isinstance(due, str) and len(due) == 8 else None)
            base = item_name or item["item"]
            item["summary"] = (
                f"{base}：剩余 {left} km"
                + (f"（{due_s} 到期）" if due_s else "")
                if isinstance(left, (int, float)) and not isinstance(left, bool)
                else (f"{base}" + (f" · {due_s}" if due_s else "")))
        maint.append(item)
    return _page(
        request, "settings.html",
        s=settings, keeper=keeper.status() if keeper else None,
        n_latest=n_latest, n_hist=n_hist, geofences=geofences,
        ota_version=ota_version, maint=maint, mcp_enabled=hasattr(request.app.state, "mcp"),
        pre_name=name, pre_lat=lat, pre_lon=lon)


# ----------------------------------------------------------------------
# 首次接车、地点管理与数据导出
# ----------------------------------------------------------------------

@router.post("/settings/vehicle")
async def bind_vehicle(request: Request):
    state = request.app.state
    form = await request.form()
    raw = str(form.get("vin") or "").strip()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", raw, re.ASCII | re.IGNORECASE):
        return RedirectResponse("/settings?err=请输入17位车架号，字母不含I、O、Q#session", 303)
    vin = raw.upper()
    if not state.vin:
        state.db.execute(
            "INSERT INTO meta(key,value) VALUES('vin',?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value WHERE meta.value=''", (vin,))
        state.vin = state.db.query_one("SELECT value FROM meta WHERE key='vin'")["value"]
        state.collection_changed.set()
    if state.vin.upper() != vin:
        return RedirectResponse("/settings?err=此数据目录已绑定其他车辆#session", 303)
    return RedirectResponse("/settings#session", 303)


@router.post("/settings/geofence/add")
async def geofence_add(request: Request):
    db: Database = request.app.state.db
    form = await request.form()
    try:
        name = str(form.get("name") or "").strip()
        lat, lon = float(form.get("lat")), float(form.get("lon"))
        radius = float(form.get("radius") or 100)
        if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180
                            and math.isfinite(radius) and radius > 0):
            raise ValueError
    except (TypeError, ValueError):
        return RedirectResponse("/settings?err=请填写有效名称、经纬度及正数半径", status_code=303)
    db.execute(
        "INSERT INTO geofences(name, lat, lon, radius_m) VALUES(?,?,?,?)",
        (name, lat, lon, radius))
    return RedirectResponse("/settings#geo", status_code=303)


@router.post("/settings/geofence/delete")
async def geofence_delete(request: Request):
    db: Database = request.app.state.db
    form = await request.form()
    try:
        fence_id = int(form.get("id") or "")
    except (ValueError, TypeError):
        return RedirectResponse("/settings?err=无效的地点", status_code=303)
    db.execute("DELETE FROM geofences WHERE id=?", (fence_id,))
    return RedirectResponse("/settings#geo", status_code=303)


@router.get("/settings/export")
def data_export(request: Request, table: str = "db"):
    """从一致的 SQLite 快照下载整库，或按表导出 CSV。"""
    db: Database = request.app.state.db
    if table == "db":
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp:
            path = Path(temp.name)
        try:
            db.backup_to(path)
            return FileResponse(path, filename="liam-export.db", media_type="application/octet-stream",
                                background=BackgroundTask(path.unlink, missing_ok=True))
        except Exception:
            path.unlink(missing_ok=True)
            raise
    sql = {"sessions": "SELECT * FROM drive_sessions WHERE vin=? ORDER BY start_ms",
           "cloud": "SELECT * FROM charge_records_cloud WHERE vin=? ORDER BY start_ms",
           "charge": "SELECT * FROM charge_sessions WHERE vin=? ORDER BY start_ms"}.get(table)
    if not sql:
        return RedirectResponse("/settings", status_code=302)
    rows = db.query_all(sql, (request.app.state.vin,))
    if not rows:
        return RedirectResponse("/settings?err=所选数据暂无可导出的记录", status_code=302)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rows[0].keys())
    for r in rows:
        w.writerow(tuple(r))
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename={table}.csv"})


@password_router.get("/login", response_class=HTMLResponse)
async def page_login(request: Request, next: str = "/"):
    target = next if next in UI_PAGES else "/"
    auth: UiAuth = request.app.state.ui_auth
    if auth.guard(request):
        return RedirectResponse(target, status_code=302)
    return templates.TemplateResponse(request, "login.html", {"err": "", "target": target})


@password_router.post("/login")
async def do_login(request: Request):
    auth: UiAuth = request.app.state.ui_auth
    form = await request.form()
    password = str(form.get("password") or "")
    target = str(form.get("next") or "/")
    if target not in UI_PAGES:
        target = "/"
    if auth.check_password(password):
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(COOKIE_NAME, auth.make_cookie(),
                        max_age=30 * 86400, httponly=True, samesite="lax")
        return resp
    return templates.TemplateResponse(
        request, "login.html", {"err": "密码不正确，请重新输入。", "target": target}, status_code=401)


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
        "ORDER BY ingested_at_ms DESC, id DESC LIMIT ?",
        (vin, path, since, limit + 1 if limit >= 0 else -1))
    truncated = 0 <= limit < len(rows)
    out = []
    for r in reversed(rows[:limit] if truncated else rows):
        v = _ps(r["value_json"])
        if isinstance(v, dict):        # 数值化：图表要标量
            v = v.get("spd") if v.get("spd") is not None else v.get("value")
        out.append({"t": r["ingested_at_ms"], "v": v})
    return JSONResponse({"path": path, "points": out, "truncated": truncated})


@router.get("/api/v1/positions.json")
async def api_positions(request: Request, session_id: int):
    db: Database = request.app.state.db
    rows = db.query_all(
        "SELECT ts_ms, lat, lon, spd, dir FROM positions "
        "WHERE vin=? AND drive_session_id=? ORDER BY ts_ms", (request.app.state.vin, session_id))
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/v1/charge-samples.json")
async def api_charge_samples(request: Request, session_id: int):
    db: Database = request.app.state.db
    rows = db.query_all(
        "SELECT ts_ms, soc, power_kw, current, voltage FROM charge_samples "
        "WHERE vin=? AND charge_session_id=? ORDER BY ts_ms", (request.app.state.vin, session_id))
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/v1/states-timeline.json")
async def api_states_timeline(request: Request, hours: int = 24):
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    since = int(time.time() * 1000) - hours * 3600 * 1000
    rows = db.query_all(
        "SELECT state, start_ms, end_ms FROM states WHERE vin=? "
        "AND (end_ms IS NULL OR end_ms>?) ORDER BY start_ms", (vin, since))
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/v1/charge-monthly.json")
async def api_charge_monthly(request: Request):
    """云端充电记录按月聚合（次数/电量，4 类型分色）。"""
    db: Database = request.app.state.db
    vin: str = request.app.state.vin
    rows = db.query_all(
        "SELECT substr(datetime(start_ms/1000, 'unixepoch', 'localtime'), 1, 7) ym, "
        "COUNT(*) n, SUM(energy_kwh) kwh FROM charge_records_cloud "
        "WHERE vin=? AND start_ms IS NOT NULL GROUP BY ym ORDER BY ym", (vin,))
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/v1/soc-histogram.json")
async def api_soc_histogram(request: Request):
    """近 90 天 SOC 变化采样的电量分布。"""
    buckets = soc_buckets(request.app.state.db, request.app.state.vin, days=90, width=10)
    return JSONResponse({"samples": sum(buckets), "buckets": buckets})


@router.get("/api/v1/vampire-drain.json")
async def api_vampire_drain(request: Request, days: int = 30):
    from ..collector.analysis import vampire_drain
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    return JSONResponse(vampire_drain(
        db, request.app.state.vin, days, settings.battery_kwh))


@router.get("/api/v1/visited.json")
async def api_visited(request: Request, days: int = 30):
    from ..collector.analysis import visited_places
    db: Database = request.app.state.db
    return JSONResponse(visited_places(db, request.app.state.vin, days))
