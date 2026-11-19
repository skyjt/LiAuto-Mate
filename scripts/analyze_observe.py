"""观察期数据分析（M1-6 定参用）：

  1. 状态判定输入的时间线（online/ChargeStatus/TravelStatus/位置/里程）
  2. ChargeStatus 值分布（标定充电态枚举）
  3. 各关键路径 ts 停更模式（休眠判定依据：车端 ts 不再前进）
  4. 推导状态转移时间线（离线段/行驶段/充电段）

用法：python scripts/analyze_observe.py [data/liam.db]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

DB = sys.argv[1] if len(sys.argv) > 1 else "data/liam.db"

KEY_PATHS = {
    "online5g": "Vehicle.ConnectManager.ConnectStatus.5G",
    "onlinexcu": "Vehicle.ConnectManager.ConnectStatus.xcu",
    "onlinehuf": "Vehicle.ConnectManager.ConnectStatus.hu-f",
    "charge": "Vehicle.Powertrain.Battery.ChargeStatus",
    "soc": "Vehicle.Powertrain.Battery.ResidueBattery",
    "travel": "Vehicle.Cabin.TravelStatus",
    "loc": "Vehicle.Location.CurrentLocationInfo",
    "trip": "Vehicle.Carcenter.Trip.Total",
}


def main() -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    print("=" * 70)
    print("① 判定输入时间线（history 变化事件，按时间）")
    print("=" * 70)
    for label, path in KEY_PATHS.items():
        rows = c.execute(
            "SELECT value_json, ts_ms, ingested_at_ms FROM state_history "
            "WHERE path=? ORDER BY ingested_at_ms", (path,)).fetchall()
        print(f"\n-- {label}（{len(rows)} 次变化）")
        for r in rows[:30]:
            import datetime
            t = datetime.datetime.fromtimestamp(r["ingested_at_ms"] / 1000)
            v = r["value_json"]
            if label == "loc":
                try:
                    d = json.loads(json.loads(v))
                    v = f"lat={d.get('lat', 0):.5f} spd={d.get('spd', 0)}"
                except Exception:
                    pass
            elif label == "trip":
                try:
                    d = json.loads(json.loads(v))
                    v = f"accMileage={d.get('accMileage')}"
                except Exception:
                    pass
            print(f"  {t:%m-%d %H:%M:%S}  {v}")

    print("\n" + "=" * 70)
    print("② ChargeStatus 值分布（充电态枚举标定）")
    print("=" * 70)
    rows = c.execute(
        "SELECT value_json, count(*) n FROM state_history WHERE path=? "
        "GROUP BY value_json", (KEY_PATHS["charge"],)).fetchall()
    for r in rows:
        print(f"  value={r['value_json']}  × {r['n']}")
    if not rows:
        cur = c.execute(
            "SELECT value_json FROM state_latest WHERE path=?",
            (KEY_PATHS["charge"],)).fetchone()
        print(f"  （无变化事件，latest 值 = {cur['value_json'] if cur else 'N/A'}）")

    print("\n" + "=" * 70)
    print("③ 关键路径 ts 新鲜度（休眠停更模式，latest 车端 ts vs 现在）")
    print("=" * 70)
    import time
    now_ms = int(time.time() * 1000)
    for label, path in KEY_PATHS.items():
        r = c.execute(
            "SELECT ts_ms, ingested_at_ms FROM state_latest WHERE path=?",
            (path,)).fetchone()
        if r and r["ts_ms"]:
            age_min = (now_ms - r["ts_ms"]) / 60000
            ing_age_min = (now_ms - r["ingested_at_ms"]) / 60000
            print(f"  {label:10s} 车端ts 落后 {age_min:8.1f} 分钟"
                  f"（上次采集 {ing_age_min:.1f} 分钟前）")

    print("\n" + "=" * 70)
    print("④ TravelStatus 值分布（行驶判定标定）")
    print("=" * 70)
    rows = c.execute(
        "SELECT value_json, count(*) n FROM state_history WHERE path=? "
        "GROUP BY value_json", (KEY_PATHS["travel"],)).fetchall()
    for r in rows:
        print(f"  value={r['value_json']}  × {r['n']}")
    cur = c.execute("SELECT value_json FROM state_latest WHERE path=?",
                    (KEY_PATHS["travel"],)).fetchone()
    print(f"  latest = {cur['value_json'] if cur else 'N/A'}")

    print("\n" + "=" * 70)
    print("⑤ 采集健康")
    print("=" * 70)
    total = c.execute("SELECT count(*) FROM collector_log").fetchone()[0]
    ok = c.execute("SELECT count(*) FROM collector_log WHERE ok=1").fetchone()[0]
    lat = c.execute("SELECT avg(latency_ms) FROM collector_log").fetchone()[0]
    print(f"  polls={total} ok={ok} 成功率={ok/total*100 if total else 0:.1f}% "
          f"平均延迟={lat:.0f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
