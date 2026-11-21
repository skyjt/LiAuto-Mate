"""Sessionizer（M2）：状态变迁驱动行车/充电会话的开闭与采样。

设计（方案 §6.4）：
  充电会话：sm → charging 时开启（记录起 SOC/位置）；每轮 T1chg 采样入
           charge_samples；离开 charging 时关闭（终 SOC/时长/峰值功率）。
  行车会话：sm → driving 时开启（记录起位置/Trip 基准）；每轮 T1loc
           位置点入 positions；离开 driving 时关闭（里程增量=Trip Δ，
           均速=里程/时长，起终点 geofence 命名）。
  geofence：起终点落在用户命名点半径内 → 会话命名「家 → 公司」。
所有写入由 Collector 在对应轮次调用 —— 本模块无循环。
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

from ..core.db import Database
from .paths import P_LOCATION, P_SOC, P_TRIP_TOTAL

log = logging.getLogger(__name__)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（米）—— geofence 匹配用。"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _parse_loc(value: Any) -> tuple[float, float] | None:
    """VSS Location 值（JSON 字符串）→ (lat, lon)；无效返回 None。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict) and value.get("v"):
        lat, lon = value.get("lat"), value.get("lon")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    return None


class Sessionizer:
    def __init__(self, db: Database, vin: str,
                 battery_kwh: float = 42.8) -> None:
        self.db = db
        self.vin = vin
        self.battery_kwh = battery_kwh
        self._drive_id: int | None = None
        self._charge_id: int | None = None
        self._trip_base_m: float | None = None
        self._drive_start_soc: float | None = None
        self._charge_peak_kw: float = 0.0
        self._charge_power_sum: float = 0.0
        self._charge_sample_n: int = 0

    # ------------------------------------------------------------------
    # geofence
    # ------------------------------------------------------------------

    def match_geofence(self, lat: float, lon: float) -> str | None:
        for g in self.db.query_all(
                "SELECT name, lat, lon, radius_m FROM geofences"):
            if haversine_m(lat, lon, g["lat"], g["lon"]) <= g["radius_m"]:
                return g["name"]
        return None

    # ------------------------------------------------------------------
    # 状态变迁钩子（由 StateMachine._record_transition 之外调用；
    # Collector 在状态变化后调用 on_state_change）
    # ------------------------------------------------------------------

    async def on_state_change(self, old: str, new: str, merged: dict[str, dict]) -> None:
        now = int(time.time() * 1000)
        # --- 关闭旧会话 ---
        if old in ("driving",) or (old == "charging" and new != "charging"):
            pass
        if old == "driving" and new != "driving":
            self._close_drive(merged, now)
        if old == "charging" and new != "charging":
            self._close_charge(merged, now)
        # --- 开启新会话 ---
        if new == "driving":
            self._open_drive(merged, now)
        if new == "charging":
            self._open_charge(merged, now)

    # ------------------------------------------------------------------
    # 行车会话
    # ------------------------------------------------------------------

    def _open_drive(self, merged: dict[str, dict], now: int) -> None:
        loc = _parse_loc(merged.get(P_LOCATION, {}).get("value"))
        trip = merged.get(P_TRIP_TOTAL, {}).get("value")
        trip_m = None
        if isinstance(trip, str):
            try:
                trip_m = float(json.loads(trip).get("accMileage", 0))
            except ValueError:
                pass
        elif isinstance(trip, dict):
            trip_m = float(trip.get("accMileage", 0) or 0)
        self._trip_base_m = trip_m
        self._drive_start_soc = _num(merged.get(P_SOC, {}).get("value"))
        gf = self.match_geofence(*loc) if loc else None
        self._drive_id = self.db.insert(
            "INSERT INTO drive_sessions(vin, start_ms, start_lat, start_lon, "
            "start_geofence, created_at_ms, updated_at_ms) VALUES(?,?,?,?,?,?,?)",
            (self.vin, now, loc[0] if loc else None, loc[1] if loc else None,
             gf, now, now))
        if self._drive_start_soc is not None:
            self.db.execute(
                "INSERT OR REPLACE INTO drive_soc_marks(drive_session_id, ts_ms, soc) "
                "VALUES(?,?,?)", (self._drive_id, now, self._drive_start_soc))
        log.info("行车会话 #%s 开启（起 %s，SOC %s%%）",
                 self._drive_id, gf or "未知位置", self._drive_start_soc)

    def _close_drive(self, merged: dict[str, dict], now: int) -> None:
        if not self._drive_id:
            return
        did = self._drive_id
        self._drive_id = None
        loc = _parse_loc(merged.get(P_LOCATION, {}).get("value"))
        trip = merged.get(P_TRIP_TOTAL, {}).get("value")
        trip_m = None
        if isinstance(trip, str):
            try:
                trip_m = float(json.loads(trip).get("accMileage", 0))
            except ValueError:
                pass
        elif isinstance(trip, dict):
            trip_m = float(trip.get("accMileage", 0) or 0)
        # 里程增量（Trip.Total.accMileage 单位待观察期标定：12500 出现过，
        # 疑似 0.1km 或累计计数——M2 联调时用一次真实行程核对）
        distance = None
        if trip_m is not None and self._trip_base_m is not None:
            distance = trip_m - self._trip_base_m
        gf = self.match_geofence(*loc) if loc else None
        row = self.db.query_one(
            "SELECT start_ms FROM drive_sessions WHERE id=?", (did,))
        duration_s = (now - row["start_ms"]) / 1000 if row else None
        avg = None
        if distance and duration_s and duration_s > 0:
            avg = distance / duration_s * 3.6
        # 能耗推导（M6-info，方案 §8.4 efficiency）：SOC 差 × 电池容量。
        # 途中充电 → SOC 差为负/失真 → 诚实记 None。
        energy = None
        efficiency = None
        end_soc = _num(merged.get(P_SOC, {}).get("value"))
        if (self._drive_start_soc is not None and end_soc is not None
                and end_soc < self._drive_start_soc):
            energy = round(
                (self._drive_start_soc - end_soc) / 100 * self.battery_kwh, 3)
            if distance and distance > 500:
                efficiency = round(energy / (distance / 1000), 2)
        if end_soc is not None:
            self.db.execute(
                "INSERT OR REPLACE INTO drive_soc_marks(drive_session_id, ts_ms, soc) "
                "VALUES(?,?,?)", (did, now, end_soc))
        self._drive_start_soc = None
        self.db.execute(
            "UPDATE drive_sessions SET end_ms=?, end_lat=?, end_lon=?, "
            "end_geofence=?, distance_m=?, duration_s=?, avg_speed_kmh=?, "
            "energy_kwh=?, efficiency_kwh_100=?, updated_at_ms=? WHERE id=?",
            (now, loc[0] if loc else None, loc[1] if loc else None, gf,
             distance, int(duration_s) if duration_s else None, avg,
             energy, efficiency, now, did))
        log.info("行车会话 #%s 关闭（终 %s，里程 %s m，能耗 %s kWh）",
                 did, gf or "?", distance, energy)

    def add_position(self, merged: dict[str, dict]) -> None:
        """T1loc 轮次：写轨迹点（会话未开则忽略——防漏判边界）。"""
        if not self._drive_id:
            return
        dp = merged.get(P_LOCATION, {})
        loc = _parse_loc(dp.get("value"))
        if not loc:
            return
        ts = dp.get("ts")
        self.db.execute(
            "INSERT INTO positions(vin, drive_session_id, ts_ms, lat, lon, spd, dir, alt) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self.vin, self._drive_id, int(ts) if isinstance(ts, (int, float))
             else int(time.time() * 1000), loc[0], loc[1], _loc_field(dp, "spd"),
             _loc_field(dp, "dir"), _loc_field(dp, "alt")))

    # ------------------------------------------------------------------
    # 充电会话
    # ------------------------------------------------------------------

    def _open_charge(self, merged: dict[str, dict], now: int) -> None:
        soc = _num(merged.get(P_SOC, {}).get("value"))
        loc = _parse_loc(merged.get(P_LOCATION, {}).get("value"))
        gf = self.match_geofence(*loc) if loc else None
        self._charge_id = self.db.insert(
            "INSERT INTO charge_sessions(vin, start_ms, start_soc, geofence, "
            "created_at_ms, updated_at_ms) VALUES(?,?,?,?,?,?)",
            (self.vin, now, soc, gf, now, now))
        self._charge_peak_kw = 0.0
        self._charge_power_sum = 0.0
        self._charge_sample_n = 0
        log.info("充电会话 #%s 开启（SOC %s%%，%s）", self._charge_id, soc, gf or "?")

    def _close_charge(self, merged: dict[str, dict], now: int) -> None:
        if not self._charge_id:
            return
        cid = self._charge_id
        self._charge_id = None
        soc = _num(merged.get(P_SOC, {}).get("value"))
        avg = (self._charge_power_sum / self._charge_sample_n
               if self._charge_sample_n else None)
        self.db.execute(
            "UPDATE charge_sessions SET end_ms=?, end_soc=?, power_peak_kw=?, "
            "power_avg_kw=?, updated_at_ms=? WHERE id=?",
            (now, soc, self._charge_peak_kw if self._charge_sample_n else None, avg, now, cid))
        log.info("充电会话 #%s 关闭（终 SOC %s%%）", cid, soc)

    def add_charge_sample(self, merged: dict[str, dict]) -> None:
        """T1chg 轮次：SOC/功率采样。"""
        if not self._charge_id:
            return
        soc = _num(merged.get(P_SOC, {}).get("value"))
        power = _num(merged.get(
            "Vehicle.Powertrain.Battery.CLTCChargePower", {}).get("value"))
        current = _num(merged.get(
            "Vehicle.Powertrain.Battery.OGCChargeCurrent", {}).get("value"))
        voltage = _num(merged.get(
            "Vehicle.Powertrain.Battery.OGCChargeVoltage", {}).get("value"))
        now = int(time.time() * 1000)
        if power is not None:
            self._charge_peak_kw = max(self._charge_peak_kw, power)
            self._charge_power_sum += power
            self._charge_sample_n += 1
        self.db.execute(
            "INSERT INTO charge_samples(vin, charge_session_id, ts_ms, soc, "
            "power_kw, current, voltage) VALUES(?,?,?,?,?,?,?)",
            (self.vin, self._charge_id, now, soc, power, current, voltage))


# ----------------------------------------------------------------------
def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _loc_field(dp: dict, field: str) -> float | None:
    try:
        raw = dp.get("value")
        v = json.loads(raw) if isinstance(raw, str) else raw
        return float(v.get(field)) if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None
