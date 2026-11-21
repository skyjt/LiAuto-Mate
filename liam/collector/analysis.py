"""数据面分析器（M6-info-2）：幽灵功耗 + 常去地点。

纯函数（db/vin/days 参数），MCP 工具与 Web API 共用。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from ..core.db import Database
from .sessionizer import haversine_m

P_SOC = "Vehicle.Powertrain.Battery.ResidueBattery"
P_LOC = "Vehicle.Location.CurrentLocationInfo"


def _ps(v_json: str | None) -> Any:
    """state value_json → python 值；VSS 嵌套对象 {id, value, timestamp}
    模式解出内层 value（VehInfo.CarCenter 族——不解包会把整坨 dict
    连 accountId 一起返回，实测踩坑 11-20）。"""
    if v_json is None:
        return None
    v = json.loads(v_json)
    if isinstance(v, str) and v[:1] in "{[":
        try:
            v = json.loads(v)
        except ValueError:
            return v
    if isinstance(v, dict) and "value" in v and "id" in v:
        inner = v["value"]
        if isinstance(inner, str) and inner[:1] in "{[":
            try:
                return json.loads(inner)
            except ValueError:
                return inner
        return inner
    return v


def switch_value(value: Any) -> int | float | None:
    """开关信号兼容数字、布尔值和嵌套 value，缺失或无法解析时返回 None。"""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value.lower())
        except ValueError:
            return None
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def soc_buckets(db: Database, vin: str, days: int, width: int) -> list[int]:
    since = int(time.time() * 1000) - days * 86400 * 1000
    rows = db.query_all(
        "SELECT value_json FROM state_history WHERE vin=? AND path=? AND ingested_at_ms>?",
        (vin, P_SOC, since))
    buckets = [0] * (100 // width)
    for row in rows:
        try:
            value = float(_ps(row["value_json"]))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 100:
            buckets[min(int(value // width), len(buckets) - 1)] += 1
    return buckets


def vampire_drain(db: Database, vin: str, days: int = 30,
                 battery_kwh: float = 42.8) -> list[dict]:
    """停车段（parked_awake/asleep 连续区段）SOC 下降 → 平均功耗。

    数据边界（如实呈现）：车辆休眠后 VSS 停更，长停车段可能只有
    起点附近的有效采样；段内 <2 个采样点或 SOC 无下降则跳过。
    """
    since = int(time.time() * 1000) - days * 86400 * 1000
    parks = db.query_all(
        "SELECT state, start_ms, end_ms FROM states WHERE vin=? "
        "AND state IN ('parked_awake','asleep') "
        "AND (end_ms IS NULL OR end_ms>?) ORDER BY start_ms",
        (vin, since))
    socs = db.query_all(
        "SELECT ingested_at_ms, value_json FROM state_history "
        "WHERE vin=? AND path=? AND ingested_at_ms>? "
        "ORDER BY ingested_at_ms", (vin, P_SOC, since))
    pts: list[tuple[int, float]] = []
    for r in socs:
        try:
            pts.append((r["ingested_at_ms"], float(_ps(r["value_json"]))))
        except (TypeError, ValueError):
            pass
    out = []
    for p in parks:
        s0 = p["start_ms"]
        s1 = p["end_ms"] or int(time.time() * 1000)
        in_seg = [v for t, v in pts if s0 <= t <= s1]
        if len(in_seg) < 2:
            continue
        drop = in_seg[0] - in_seg[-1]
        dur_h = (s1 - s0) / 3600000
        if drop <= 0 or dur_h < 0.5:
            continue
        out.append({
            "state": p["state"],
            "start_ms": s0, "end_ms": p["end_ms"],
            "hours": round(dur_h, 1),
            "soc_drop_pct": drop,
            "kwh_lost": round(drop / 100 * battery_kwh, 3),
            "avg_kw": round(drop / 100 * battery_kwh / dur_h, 4),
        })
    out.sort(key=lambda x: -x["start_ms"])
    return out[:50]


def visited_places(db: Database, vin: str, days: int = 30,
                   cluster_m: float = 200.0) -> list[dict]:
    """常去地点：parked_awake 段起点时刻的最近位置 → 半径聚类 → 高频点。

    返回按停留次数降序；命中 geofence 的带 name。
    """
    since = int(time.time() * 1000) - days * 86400 * 1000
    parks = db.query_all(
        "SELECT start_ms FROM states WHERE vin=? AND state='parked_awake' "
        "AND start_ms>? ORDER BY start_ms", (vin, since))
    coords: list[tuple[float, float, int]] = []
    for p in parks:
        row = db.query_one(
            "SELECT value_json FROM state_history WHERE vin=? AND path=? "
            "AND ingested_at_ms<=? ORDER BY ingested_at_ms DESC LIMIT 1",
            (vin, P_LOC, p["start_ms"]))
        if not row:
            continue
        try:
            loc = _ps(row["value_json"])
            if isinstance(loc, dict) and loc.get("v"):
                coords.append((float(loc["lat"]), float(loc["lon"]),
                               p["start_ms"]))
        except (TypeError, ValueError, KeyError):
            pass
    clusters: list[dict] = []
    for lat, lon, ts in coords:
        hit = None
        for c in clusters:
            if haversine_m(lat, lon, c["lat"], c["lon"]) <= cluster_m:
                hit = c
                break
        if hit:
            hit["n"] += 1
            hit["last_ms"] = max(hit["last_ms"], ts)
        else:
            clusters.append({"lat": lat, "lon": lon, "n": 1,
                             "first_ms": ts, "last_ms": ts})
    fences = db.query_all("SELECT name, lat, lon, radius_m FROM geofences")
    for c in clusters:
        c["name"] = next(
            (g["name"] for g in fences
             if haversine_m(c["lat"], c["lon"], g["lat"], g["lon"])
             <= g["radius_m"]), None)
    clusters.sort(key=lambda c: -c["n"])
    return clusters[:20]
