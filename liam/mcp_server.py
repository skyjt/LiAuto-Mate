"""MCP 服务器（M4，方案 §8.3）：/mcp 只读工具，Agent 获取车辆信息。

  · streamable HTTP 挂同一 FastAPI（单容器单端口不变）
  · LIAM_MCP_TOKEN Bearer 必需（未设=整体关闭，由 app 装配判断）
  · 全部只读；返回带 数值+单位+车端 ts+stale 标志（Agent 必须能判断
    「车辆休眠中，数据截至 xx:xx」）；手机号/凭据永不出现
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .core.db import Database


def mount_mcp(app, db: Database, keeper_ref: dict,
              settings: Settings) -> None:
    """构建 MCP Starlette 子应用并挂载到 FastAPI /mcp。"""
    mcp = FastMCP(
        "liauto-mate",
        instructions="Li Auto (理想) vehicle data. All tools read-only. "
                      "Values carry unit + vehicle timestamp + stale flag; "
                      "stale=true means the car is asleep/offline and data "
                      "is last-known.",
        streamable_http_path="/",   # 挂载点 /mcp 由外层 FastAPI mount 提供
    )

    # ------------------------------------------------------------------
    # 共享分析器（liam.collector.analysis，MCP 与 Web API 复用）
    # ------------------------------------------------------------------
    from .collector.analysis import _ps, soc_buckets, switch_value, vampire_drain, visited_places

    def _latest(path: str) -> Any:
        r = db.query_one(
            "SELECT value_json, ts_ms, ingested_at_ms FROM state_latest "
            "WHERE vin=? AND path=?", (app.state.vin, path))
        return r

    def _num_of(path: str) -> tuple[Any, int | None, bool]:
        r = _latest(path)
        if not r:
            return None, None, True
        stale = (time.time() * 1000 - (r["ingested_at_ms"] or 0)) > 20 * 60 * 1000
        return _ps(r["value_json"]), r["ts_ms"], stale

    # ------------------------------------------------------------------
    @mcp.tool()
    def get_vehicle_status() -> dict:
        """实时快照：状态徽章/SOC/续航/位置+速度/门锁/胎压/温度/充电功率。"""
        online = switch_value(_num_of("Vehicle.ConnectManager.ConnectStatus.5G")[0])
        locked = switch_value(_num_of("Vehicle.Body.DoorLockStatus.MainDoor")[0])
        soc, soc_ts, soc_stale = _num_of(
            "Vehicle.Powertrain.Battery.ResidueBattery")
        loc, _, _ = _num_of("Vehicle.Location.CurrentLocationInfo")
        st = db.query_one(
            "SELECT state, start_ms FROM states WHERE vin=? AND end_ms IS NULL",
            (app.state.vin,))
        power, _, _ = _num_of("Vehicle.Powertrain.Battery.CLTCChargePower")
        out = {
            "state": st["state"] if st else "unknown",
            "online": None if online is None else bool(online),
            "soc_percent": soc, "soc_ts": soc_ts, "soc_stale": soc_stale,
            "location": loc if isinstance(loc, dict) else None,
            "charge_power_kw": power,
            "doors_locked": None if locked is None else bool(locked),
        }
        for k, p in (("range_elec_km", "Vehicle.Cabin.CLTC.PureElecEnduranceMileInd"),
                     ("range_fuel_km", "Vehicle.Cabin.CLTC.FuelEnduranceMileInd"),
                     ("cabin_temp_c", "Vehicle.Cabin.AC.FrtACIncarTemp"),
                     ("sentry_on", "Vehicle.Sentry.SentinelStatus")):
            v, ts, stale = _num_of(p)
            out[k] = {"value": v, "ts": ts, "stale": stale}
        # 车外温度十分度（164.0 = 16.4℃）——与 Web 概览同规则
        v, ts, stale = _num_of("Vehicle.VehInfo.CarControl.Hvac.DspOtsdTemp")
        try:
            v = round(float(v) / 10, 1) if v is not None else None
        except (TypeError, ValueError):
            v = None
        out["outside_temp_c"] = {"value": v, "ts": ts, "stale": stale}
        return out

    @mcp.tool()
    def get_state_group(prefix: str) -> dict:
        """按前缀分组查状态（如 'Vehicle.Chassis.Tire' / 'Vehicle.Cabin.AC'）。

        返回该前缀下全部路径的 {value, ts, stale}。
        """
        rows = db.query_all(
            "SELECT path, value_json, ts_ms, ingested_at_ms FROM state_latest "
            "WHERE vin=? AND path LIKE ?", (app.state.vin, prefix + "%"))
        out = {}
        for r in rows:
            stale = (time.time() * 1000 - (r["ingested_at_ms"] or 0)) > 20 * 60 * 1000
            out[r["path"]] = {"value": _ps(r["value_json"]),
                              "ts": r["ts_ms"], "stale": stale}
        return out

    @mcp.tool()
    def get_signal_history(path: str, hours: int = 24, limit: int = 500) -> dict:
        """任意 VSS 路径的历史时序（change-only，本地采集）。"""
        since = int(time.time() * 1000) - hours * 3600 * 1000
        rows = db.query_all(
            "SELECT ts_ms, ingested_at_ms, value_json FROM state_history "
            "WHERE vin=? AND path=? AND ingested_at_ms>? "
            "ORDER BY ingested_at_ms DESC, id DESC LIMIT ?", (app.state.vin, path, since, limit))
        return {"path": path, "points": [
            {"t": r["ingested_at_ms"], "v": _ps(r["value_json"])} for r in reversed(rows)]}

    @mcp.tool()
    def get_drive_sessions(limit: int = 20) -> list:
        """行车会话列表（本地推导：起止/里程/时长/均速/起终点命名）。"""
        rows = db.query_all(
            "SELECT * FROM drive_sessions WHERE vin=? ORDER BY start_ms DESC "
            "LIMIT ?", (app.state.vin, limit))
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_drive_route(session_id: int) -> list:
        """单次行车轨迹点序列（WGS-84，lat/lon/spd/dir/alt）。"""
        rows = db.query_all(
            "SELECT ts_ms, lat, lon, spd, dir, alt FROM positions "
            "WHERE vin=? AND drive_session_id=? ORDER BY ts_ms", (app.state.vin, session_id))
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_charge_sessions(limit: int = 20) -> list:
        """充电会话列表（本地推导：起止 SOC/峰值功率/地点）。"""
        rows = db.query_all(
            "SELECT * FROM charge_sessions WHERE vin=? ORDER BY start_ms DESC "
            "LIMIT ?", (app.state.vin, limit))
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_charge_records_cloud(limit: int = 50) -> list:
        """云端充电记录（可回填历史，含充电类型）。"""
        rows = db.query_all(
            "SELECT record_id, start_ms, end_ms, energy_kwh, charging_type "
            "FROM charge_records_cloud WHERE vin=? ORDER BY start_ms DESC "
            "LIMIT ?", (app.state.vin, limit))
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_efficiency_stats() -> list:
        """月度里程（云端口径，travel_daily 月度键 YYYY-MM）。"""
        rows = db.query_all(
            "SELECT date, mileage_km, energy_kwh FROM travel_daily "
            "WHERE vin=? AND date LIKE '20%' ORDER BY date", (app.state.vin,))
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_geofences() -> list:
        """常去地点（用户命名，如「家」「公司」）。"""
        rows = db.query_all(
            "SELECT name, lat, lon, radius_m FROM geofences")
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_states_timeline(hours: int = 48) -> list:
        """车辆状态时间线（driving/charging/parked/asleep 区段）。"""
        since = int(time.time() * 1000) - hours * 3600 * 1000
        rows = db.query_all(
            "SELECT state, start_ms, end_ms FROM states WHERE vin=? "
            "AND (end_ms IS NULL OR end_ms>?) ORDER BY start_ms",
            (app.state.vin, since))
        return [dict(r) for r in rows]

    @mcp.tool()
    def get_battery_health() -> dict:
        """宏观电池健康：SOC 分布（近 30 天充电深度）+ 月度充电统计。"""
        counts = soc_buckets(db, app.state.vin, days=30, width=20)
        buckets = dict(zip(("0-20", "20-40", "40-60", "60-80", "80-100"), counts, strict=True))
        months = db.query_all(
            "SELECT date, mileage_km FROM travel_daily WHERE vin=? "
            "AND date LIKE '20%' ORDER BY date DESC LIMIT 12", (app.state.vin,))
        return {"soc_samples_30d": sum(counts), "soc_distribution": buckets,
                "monthly_mileage_km": [dict(r) for r in months]}

    @mcp.tool()
    def get_ota_updates() -> dict:
        """OTA 版本/状态 + 保养计划（VSS 快照，T3 慢档每日采集）。

        ★ 实测边界：云端 ssp-as-mobile-api 的 ota/maint 端点 livis 通道
        无权限（100012）——数据源是 VSS 路径（Vehicle.OTA.* /
        Vehicle.Carcenter.*），非云端接口。
        """
        def _snap(prefixes: tuple[str, ...]) -> dict:
            out = {}
            for pfx in prefixes:
                rows = db.query_all(
                    "SELECT path, value_json, ts_ms FROM state_latest "
                    "WHERE vin=? AND path LIKE ?", (app.state.vin, pfx + "%"))
                for r in rows:
                    out[r["path"]] = {
                        "value": _ps(r["value_json"]), "ts": r["ts_ms"]}
            return out
        return {
            "ota": _snap(("Vehicle.OTA.", "Vehicle.Version.OTA.")),
            "maintenance": _snap(("Vehicle.Carcenter.Maintain.",
                                  "Vehicle.Carcenter.Aftersales.")),
        }

    @mcp.tool()
    def get_vampire_drain(days: int = 30) -> list:
        """幽灵功耗（静置电耗）：停车段 SOC 下降 → 平均功耗 kW/段。

        长时间停车（含休眠）数据可能因 VSS 停更而不完整——如实呈现。
        """
        return vampire_drain(db, app.state.vin, days, settings.battery_kwh)

    @mcp.tool()
    def get_visited_places(days: int = 30) -> list:
        """常去地点（自动统计停车高频点，200m 聚类）。"""
        return visited_places(db, app.state.vin, days)

    @mcp.tool()
    def get_system_health() -> dict:
        """采集器/会话状态/重登倒计时/最近错误。"""
        keeper = keeper_ref.get("keeper")
        total = db.query_one("SELECT COUNT(*) c FROM collector_log")["c"]
        ok = db.query_one("SELECT COUNT(*) c FROM collector_log WHERE ok=1")["c"]
        return {
            "session": keeper.status() if keeper else None,
            "poll_total": total, "poll_ok": ok,
            "success_rate": round(ok / total * 100, 2) if total else None,
        }

    # ------------------------------------------------------------------
    # 挂载：标准 FastAPI mount（子应用内部路由已改为 "/"）
    #      + Bearer 认证中间件（子应用层，先于路由）
    # ------------------------------------------------------------------
    sub = mcp.streamable_http_app()
    token = settings.mcp_token

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class BearerGate(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    sub.add_middleware(BearerGate)
    app.mount("/mcp", sub, name="mcp")
    # ★ mount 的子应用 lifespan 不会自动运行——session_manager.run()
    #   必须由宿主 lifespan 驱动（cli.serve 已对接 app.state.mcp）
    app.state.mcp = mcp
