"""Sessionizer 单测：会话开闭/轨迹点/充电采样/geofence 命名（无网络）。"""

from __future__ import annotations

import json

import pytest

from liam.collector.paths import P_LOCATION, P_SOC, P_TRIP_TOTAL
from liam.collector.sessionizer import Sessionizer, haversine_m
from liam.collector.state_machine import S_CHARGING, S_DRIVING, S_PARKED_AWAKE
from liam.core.db import Database


def _merged(soc=30, lat=31.5, lon=120.3, spd=0.0, trip=12500.0):
    return {
        P_SOC: {"value": soc},
        P_LOCATION: {"value": json.dumps(
            {"lat": lat, "lon": lon, "spd": spd, "v": True})},
        P_TRIP_TOTAL: {"value": json.dumps({"accMileage": trip})},
        "Vehicle.Powertrain.Battery.CLTCChargePower": {"value": 60.0},
        "Vehicle.Powertrain.Battery.OGCChargeCurrent": {"value": 120.0},
        "Vehicle.Powertrain.Battery.OGCChargeVoltage": {"value": 400.0},
    }


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "s.db")


async def test_drive_session_open_close(db):
    sz = Sessionizer(db, "V1")
    await sz.on_state_change(S_PARKED_AWAKE, S_DRIVING, _merged())
    # 行驶中打点
    sz.add_position(_merged(spd=45))
    sz.add_position(_merged(spd=50))
    # 结束（Trip 增量 2.5 → 2500 m 单位待标定，先验证差值逻辑）
    await sz.on_state_change(S_DRIVING, S_PARKED_AWAKE, _merged(trip=12502.5))
    row = db.query_one(
        "SELECT * FROM drive_sessions WHERE vin='V1'")
    assert row["start_ms"] is not None and row["end_ms"] is not None
    assert row["distance_m"] == 2.5        # 增量差值（单位标定在联调）
    n_pos = db.query_one(
        "SELECT COUNT(*) c FROM positions WHERE drive_session_id=?",
        (row["id"],))["c"]
    assert n_pos == 2


async def test_charge_session_with_samples(db):
    sz = Sessionizer(db, "V1")
    await sz.on_state_change(S_PARKED_AWAKE, S_CHARGING, _merged(soc=20))
    sz.add_charge_sample(_merged(soc=22))
    sz.add_charge_sample(_merged(soc=25))
    await sz.on_state_change(S_CHARGING, S_PARKED_AWAKE, _merged(soc=80))
    row = db.query_one("SELECT * FROM charge_sessions WHERE vin='V1'")
    assert row["start_soc"] == 20 and row["end_soc"] == 80
    assert row["power_peak_kw"] == 60.0
    n = db.query_one(
        "SELECT COUNT(*) c FROM charge_samples WHERE charge_session_id=?",
        (row["id"],))["c"]
    assert n == 2


async def test_geofence_naming(db):
    db.execute(
        "INSERT INTO geofences(name, lat, lon, radius_m) VALUES(?,?,?,?)",
        ("家", 31.5, 120.3, 200))
    db.execute(
        "INSERT INTO geofences(name, lat, lon, radius_m) VALUES(?,?,?,?)",
        ("公司", 31.6, 120.4, 200))
    sz = Sessionizer(db, "V1")
    # 从家出发
    await sz.on_state_change(S_PARKED_AWAKE, S_DRIVING, _merged(lat=31.5, lon=120.3))
    # 到公司结束
    await sz.on_state_change(S_DRIVING, S_PARKED_AWAKE,
                              _merged(lat=31.6, lon=120.4))
    row = db.query_one("SELECT * FROM drive_sessions WHERE vin='V1'")
    assert row["start_geofence"] == "家"
    assert row["end_geofence"] == "公司"


def test_haversine():
    # ~111 m per 0.001 lat
    d = haversine_m(31.5, 120.3, 31.501, 120.3)
    assert 100 < d < 120
