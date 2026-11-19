"""四态状态机单测：纯逻辑，无网络（fixture 判定输入）。"""

from __future__ import annotations

import json

from liam.collector.paths import (
    P_CHARGE_STATUS,
    P_LOCATION,
    P_ONLINE_5G,
    P_TRAVEL_STATUS,
    P_TRIP_TOTAL,
)
from liam.collector.state_machine import (
    S_ASLEEP,
    S_CHARGING,
    S_DRIVING,
    S_PARKED_AWAKE,
    StateMachine,
    VehicleState,
)
from liam.core.db import Database


def _merged(online=True, charge=15, spd=0.0, travel=0, trip=100.0, ts=1793850000000):
    return {
        P_ONLINE_5G: {"value": online, "ts": ts},
        P_CHARGE_STATUS: {"value": charge, "ts": ts},
        P_LOCATION: {"value": json.dumps(
            {"lat": 31.5, "lon": 120.3, "spd": spd, "v": True}), "ts": ts},
        P_TRAVEL_STATUS: {"value": travel, "ts": ts},
        P_TRIP_TOTAL: {"value": json.dumps({"accMileage": trip}), "ts": ts},
    }


def _mk_sm(tmp_path) -> StateMachine:
    db = Database(tmp_path / "sm.db")
    return StateMachine(db, "VIN_TEST")


def test_parked_when_idle(tmp_path):
    sm = _mk_sm(tmp_path)
    assert sm.update(VehicleState(_merged(), None)) == S_PARKED_AWAKE


def test_driving_on_speed(tmp_path):
    sm = _mk_sm(tmp_path)
    assert sm.update(VehicleState(_merged(spd=42.0), None)) == S_DRIVING


def test_charging_priority_over_parked(tmp_path):
    sm = _mk_sm(tmp_path)
    # 充电态（ChargeStatus ≠ 15）优先于静止
    assert sm.update(VehicleState(_merged(charge=2), None)) == S_CHARGING


def test_asleep_needs_confirm_rounds(tmp_path):
    """离线 + ts 停更：连续 2 轮才判休眠（防单次抖动）。"""
    sm = _mk_sm(tmp_path)
    offline = _merged(online=False, ts=1793850000000)
    s1 = sm.update(VehicleState(offline, prev_ts_ms=1793850000000 + 60000))
    assert s1 == S_PARKED_AWAKE          # 第 1 轮不判
    s2 = sm.update(VehicleState(offline, prev_ts_ms=1793850000000 + 60000))
    assert s2 == S_ASLEEP                # 第 2 轮确认


def test_drive_then_still_hysteresis(tmp_path):
    """行驶→静止：需连续 3 轮静止才离开 driving（5min 滞回）。"""
    sm = _mk_sm(tmp_path)
    sm.update(VehicleState(_merged(spd=60), None))
    stopped = _merged(spd=0.0)
    r1 = sm.update(VehicleState(stopped, None))
    r2 = sm.update(VehicleState(stopped, None))
    assert r1 == S_DRIVING and r2 == S_DRIVING   # 滞回中
    r3 = sm.update(VehicleState(stopped, None))
    assert r3 == S_PARKED_AWAKE                  # 第 3 轮确认静止


def test_states_table_timeline(tmp_path):
    """状态变迁写 states 时间线（TeslaMate states 同构）。"""
    db = Database(tmp_path / "sm.db")
    sm = StateMachine(db, "VIN_T")
    sm.update(VehicleState(_merged(spd=50), None))       # → driving
    sm.update(VehicleState(_merged(spd=0), None))         # 滞回仍 driving
    sm.update(VehicleState(_merged(spd=0), None))
    sm.update(VehicleState(_merged(spd=0), None))         # → parked
    rows = db.query_all(
        "SELECT state, start_ms, end_ms FROM states ORDER BY start_ms")
    states = [r["state"] for r in rows]
    assert states == [S_DRIVING, S_PARKED_AWAKE]
    # 旧段已闭合，新段 open
    assert rows[0]["end_ms"] is not None
    assert rows[1]["end_ms"] is None


def test_bootstrap_restores_open_state(tmp_path):
    db = Database(tmp_path / "sm.db")
    sm = StateMachine(db, "VIN_T")
    sm.update(VehicleState(_merged(charge=3), None))      # → charging
    sm2 = StateMachine(db, "VIN_T")                        # 模拟重启
    sm2.bootstrap_from_db()
    assert sm2.state == S_CHARGING
