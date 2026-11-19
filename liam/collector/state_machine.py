"""四态状态机（方案 §6.2）：driving / charging / parked_awake / asleep。

判定输入（每轮全量批自带，无额外请求）：
  · online   = ConnectStatus.5G（主判据；xcu/huf 辅助）
  · charging = ChargeStatus ∈ 充电态集合（观察期标定，见 CHARGE_ACTIVE_VALUES）
  · driving  = TravelStatus 表示行驶 / 位置 spd>0 / Trip.Total 递增
滞回防抖（§13.2：观察期后定参，当前用设计默认值）：
  · 静止确认：连续 STILL_CONFIRM_ROUNDS 轮静止 → parked_awake
  · 休眠确认：连续 SLEEP_CONFIRM_ROUNDS 轮离线/ts 停更 → asleep
状态每次变迁写 states 表（TeslaMate states 同构）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..core.db import Database
from .paths import (
    P_CHARGE_STATUS,
    P_LOCATION,
    P_ONLINE_5G,
    P_TRAVEL_STATUS,
    P_TRIP_TOTAL,
)
from .ts_layers import P_ALWAYS_FRESH

log = logging.getLogger(__name__)

S_DRIVING = "driving"
S_CHARGING = "charging"
S_PARKED_AWAKE = "parked_awake"
S_ASLEEP = "asleep"

# --- 观察期标定项（§13.2）：当前占位，48h 数据后修正 ---
# ChargeStatus 实车静止值=15。充电态枚举待观测一次完整充电标定；
# 先用「非静止值」的宽松策略：≠15 且 ≠ 缺失 → 视为充电相关，进 charging
CHARGE_IDLE_VALUE = 15

# 滞回轮数（60s/轮 → 5min/3 轮）
STILL_CONFIRM_ROUNDS = 3     # 行驶→静止确认（设计值：静止+P 持续 5min）
SLEEP_CONFIRM_ROUNDS = 2     # awake→asleep 确认（设计值：连续 2 次探测离线）


def _parse_maybe_json(v: Any) -> Any:
    """VSS 值常是 JSON 字符串（如 Location）；解析失败原样返回。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


class VehicleState:
    """一轮快照的判定输入（从 merged poll 结果构造）。"""

    __slots__ = ("online", "charging", "driving", "speed_kmh", "trip_m",
                 "loc_valid", "ts_fresh", "raw")

    def __init__(self, merged: dict[str, dict], prev_ts_ms: int | None) -> None:
        self.raw = merged
        get = lambda p: merged.get(p, {}).get("value")   # noqa: E731

        # 在线：5G 在线（值可能是 JSON 字符串包 dict 或 bool/数字）
        v5g = _parse_maybe_json(get(P_ONLINE_5G))
        if isinstance(v5g, dict):
            v5g = v5g.get("value")
        self.online = bool(v5g) if v5g is not None else False

        # 充电：ChargeStatus ≠ 静止值（待观察期标定精确枚举）
        cs = get(P_CHARGE_STATUS)
        try:
            cs_num = float(_parse_maybe_json(cs))
        except (TypeError, ValueError):
            cs_num = None
        self.charging = cs_num is not None and cs_num != CHARGE_IDLE_VALUE

        # 位置/速度/里程
        loc = _parse_maybe_json(get(P_LOCATION))
        self.loc_valid = isinstance(loc, dict) and bool(loc.get("v"))
        self.speed_kmh = float(loc.get("spd", 0) or 0) if self.loc_valid else 0.0
        trip = _parse_maybe_json(get(P_TRIP_TOTAL))
        self.trip_m = float(trip.get("accMileage", 0) or 0) if isinstance(trip, dict) else 0.0

        # 行驶：TravelStatus 非零 / 速度>0
        ts_val = _parse_maybe_json(get(P_TRAVEL_STATUS))
        try:
            travel_num = float(ts_val)
        except (TypeError, ValueError):
            travel_num = None
        self.driving = (travel_num not in (None, 0.0)) or self.speed_kmh > 5

        # ts 新鲜度（观察期发现修正）：只看「持续上报型」路径（5G/Trip.Total）
        # —— 变化才上报型（ChargeStatus/Location）静止时 ts 停更是正常现象
        max_ts = 0
        for path, dp in merged.items():
            if path not in P_ALWAYS_FRESH:
                continue
            t = dp.get("ts")
            if isinstance(t, (int, float)) and t > max_ts:
                max_ts = int(t)
        self.ts_fresh = (prev_ts_ms is None) or (max_ts > prev_ts_ms) or max_ts == 0


class StateMachine:
    """滞回状态机 + states 时间线落库。"""

    def __init__(self, db: Database, vin: str) -> None:
        self.db = db
        self.vin = vin
        self.state = S_PARKED_AWAKE
        self._still_rounds = 0
        self._sleep_rounds = 0
        self._last_trip_m = 0.0
        self._prev_max_ts: int | None = None
        self._round = 0

    # ------------------------------------------------------------------
    def update(self, vs: VehicleState) -> str:
        """喂一轮判定输入 → 返回（可能变迁后的）当前状态。"""
        self._round += 1
        prev = self.state
        new = self._decide(vs)
        if new != prev:
            self._record_transition(prev, new)
            self.state = new
            self._still_rounds = 0
            self._sleep_rounds = 0
        # ts 水位更新
        max_ts = 0
        for dp in vs.raw.values():
            t = dp.get("ts")
            if isinstance(t, (int, float)) and t > max_ts:
                max_ts = int(t)
        if max_ts:
            self._prev_max_ts = max_ts
        if vs.trip_m:
            self._last_trip_m = vs.trip_m
        return self.state

    def _decide(self, vs: VehicleState) -> str:
        # ① 离线 + ts 停更 → 休眠候选（连续 N 轮确认）
        if not vs.online and not vs.ts_fresh:
            self._sleep_rounds += 1
            if self._sleep_rounds >= SLEEP_CONFIRM_ROUNDS:
                return S_ASLEEP
            return self.state    # 未确认：保持
        self._sleep_rounds = 0

        # ② 充电态优先（行驶与充电互斥——车动不了）
        if vs.charging:
            return S_CHARGING

        # ③ 行驶
        if vs.driving or vs.speed_kmh > 5:
            self._still_rounds = 0
            return S_DRIVING

        # ④ 静止确认（连续 N 轮）
        self._still_rounds += 1
        if self.state == S_DRIVING and self._still_rounds < STILL_CONFIRM_ROUNDS:
            return S_DRIVING       # 滞回：刚停还没确认
        return S_PARKED_AWAKE

    # ------------------------------------------------------------------
    def _record_transition(self, old: str, new: str) -> None:
        now = int(time.time() * 1000)
        with self.db.transaction():
            # 关旧段
            self.db.execute(
                "UPDATE states SET end_ms=? WHERE vin=? AND end_ms IS NULL",
                (now, self.vin))
            # 开新段
            self.db.execute(
                "INSERT INTO states(vin, state, start_ms) VALUES(?,?,?)",
                (self.vin, new, now))
        log.info("状态变迁 %s → %s", old, new)

    def bootstrap_from_db(self) -> None:
        """进程重启：从 states 表恢复当前状态（无未闭段=parked_awake）。"""
        row = self.db.query_one(
            "SELECT state FROM states WHERE vin=? AND end_ms IS NULL "
            "ORDER BY start_ms DESC LIMIT 1", (self.vin,))
        self.state = row["state"] if row else S_PARKED_AWAKE
