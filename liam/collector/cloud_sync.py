"""云端历史同步器（M3）：travel 聚合 + chargeRecords 幂等镜像（方案 §4.2/§7）。

  · 每日 04:00（T4）跑一次；也可手动触发。
  · travel_daily：近 90 天日聚合 upsert（venv 字段名以实测响应为准，
    raw_json 全存兜底 —— 首次真实验证后修正列映射）。
  · chargeRecords：翻页拉全量，record_id 幂等 upsert。
  · 全部失败不致命（下一轮再试），单端点失败不影响其他。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..auth.li_api import AuthExpiredError, LiApi, LiApiError
from ..core.db import Database

log = logging.getLogger(__name__)


class CloudSync:
    def __init__(self, api: LiApi, db: Database, vin: str,
                 retention_days: int = 90) -> None:
        self.api = api
        self.db = db
        self.vin = vin
        self.retention_days = retention_days
        self.last_run_ms = 0

    # ------------------------------------------------------------------

    async def sync_all(self) -> dict[str, Any]:
        """全量同步（T4 / 手动）。返回各端点结果摘要。

        ★ 实测边界（11-20）：ota/upgrade 与 maint/plan 端点 livis 通道
        无权限（100012 没有接口访问权限——ha-lixiang 也只登记常量从未
        调用）——OTA/保养信息改走 VSS 路径（paths.T3 档），不走云端。
        """
        out: dict[str, Any] = {}
        out["travel_daily"] = await self._sync_travel_daily()
        out["charge_records"] = await self._sync_charge_records()
        self.last_run_ms = int(time.time() * 1000)
        return out

    # ------------------------------------------------------------------

    async def _sync_travel_daily(self) -> int:
        """travel 同步（实测修正 11-19）：daily 端点是**区间汇总**（不按天拆）。

        策略改为：
          ① get_travel_months → 各月 travelMil 入 travel_daily（date=月键 YYYY-MM）
          ② 近 30 天 daily 区间汇总存 raw（date='__range30'，供效率页用）
        单日明细不逐日拉（一天一请求不划算）；效率主数据用月度+区间。
        """
        import datetime
        n = 0
        # ① 月度
        data = await self.api.get_travel_months()
        for y in (data or {}).get("yearEnergyList") or []:
            for m in y.get("monthlyEnergyList") or []:
                date = f"{y.get('year')}-{int(m.get('month') or 0):02d}"
                self.db.execute(
                    "INSERT INTO travel_daily(vin,date,mileage_km,energy_kwh,raw_json) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(vin,date) DO UPDATE SET "
                    "mileage_km=excluded.mileage_km, energy_kwh=excluded.energy_kwh, "
                    "raw_json=excluded.raw_json",
                    (self.vin, date, _f(m.get("travelMil")), None,
                     json.dumps(m, ensure_ascii=False)))
                n += 1
        # ② 近 30 天区间汇总
        end = datetime.date.today()
        start = end - datetime.timedelta(days=30)
        data = await self.api.get_travel_daily(start.isoformat(), end.isoformat())
        if isinstance(data, dict):
            self.db.execute(
                "INSERT INTO travel_daily(vin,date,mileage_km,energy_kwh,raw_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(vin,date) DO UPDATE SET "
                "mileage_km=excluded.mileage_km, energy_kwh=excluded.energy_kwh, "
                "raw_json=excluded.raw_json",
                (self.vin, "__range30", _f(data.get("travelMileage")),
                 _f(data.get("elecEnergy")),
                 json.dumps(data, ensure_ascii=False)))
            n += 1
        log.info("travel 同步 %d 条（月度+30d 区间）", n)
        return n

    async def _sync_charge_records(self) -> int:
        """chargeRecords 翻页全量 → charge_records_cloud 幂等 upsert。"""
        n = 0
        page = 1
        now = int(time.time() * 1000)
        while page <= 20:      # 防御上限：20 页 × 50 = 1000 条
            data = await self.api.get_charge_records(page=page, page_size=50)
            items = _iter_items(data)
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                start = _field(item, "startTime", "startTimeMs")
                rid = _field(item, "id", "recordId", "chargeId")
                if rid is None:
                    if start is None:
                        continue  # 缺少记录 ID 与开始时间，无法识别重复记录
                    rid = start
                self.db.execute(
                    "INSERT INTO charge_records_cloud(vin,record_id,start_ms,"
                    "end_ms,energy_kwh,charging_type,raw_json,synced_at_ms) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET "
                    "start_ms=COALESCE(excluded.start_ms, start_ms), "
                    "end_ms=COALESCE(excluded.end_ms, end_ms), "
                    "energy_kwh=COALESCE(excluded.energy_kwh, energy_kwh), "
                    "charging_type=COALESCE(excluded.charging_type, charging_type), "
                    "raw_json=excluded.raw_json, synced_at_ms=excluded.synced_at_ms "
                    "WHERE charge_records_cloud.vin=excluded.vin",
                    (self.vin, str(rid),
                     _ms(start), _ms(_field(item, "endTime", "endTimeMs")),
                     _f(_field(item, "chargingCapacity", "capacity", "electricity")),
                     _field(item, "chargingType", "type"),
                     json.dumps(item, ensure_ascii=False), now))
                n += 1
            if len(items) < 50:
                break
            page += 1
        log.info("charge_records 同步 %d 条", n)
        return n

    # ------------------------------------------------------------------
    async def _sync_ota(self) -> int:
        """OTA 状态镜像（单行 upsert；字段首验后细化，raw 全存）。"""
        data = await self.api.get_ota_upgrade()
        now = int(time.time() * 1000)
        version = None
        status = None
        if isinstance(data, dict):
            version = (data.get("version") or data.get("currentVersion")
                       or (data.get("upgradeInfo") or {}).get("version"))
            status = (data.get("status") or data.get("upgradeStatus"))
        self.db.execute(
            "INSERT INTO ota_updates(vin, version, status, raw_json, synced_at_ms) "
            "VALUES(?,?,?,?,?) ON CONFLICT(vin) DO UPDATE SET "
            "version=excluded.version, status=excluded.status, "
            "raw_json=excluded.raw_json, synced_at_ms=excluded.synced_at_ms",
            (self.vin, str(version) if version else None,
             str(status) if status else None,
             json.dumps(data, ensure_ascii=False) if data is not None else None,
             now))
        log.info("OTA 同步: version=%s status=%s", version, status)
        return 1

    async def _sync_maint(self) -> int:
        """保养计划镜像（按保养项 upsert；响应结构首验后细化）。"""
        data = await self.api.get_maint_plan()
        now = int(time.time() * 1000)
        n = 0
        items = _iter_items(data)
        if not items and isinstance(data, dict):
            items = [data]           # 单对象响应也存一行（item_key='__all'）
        for item in items:
            if not isinstance(item, dict):
                continue
            key = (item.get("key") or item.get("name")
                   or item.get("type") or "__all")
            self.db.execute(
                "INSERT INTO maintenance_plan(vin, item_key, raw_json, synced_at_ms) "
                "VALUES(?,?,?,?) ON CONFLICT(vin, item_key) DO UPDATE SET "
                "raw_json=excluded.raw_json, synced_at_ms=excluded.synced_at_ms",
                (self.vin, str(key),
                 json.dumps(item, ensure_ascii=False), now))
            n += 1
        if not n:
            self.db.execute(
                "INSERT INTO maintenance_plan(vin, item_key, raw_json, synced_at_ms) "
                "VALUES(?,?,?,?) ON CONFLICT(vin, item_key) DO UPDATE SET "
                "raw_json=excluded.raw_json, synced_at_ms=excluded.synced_at_ms",
                (self.vin, "__raw",
                 json.dumps(data, ensure_ascii=False) if data is not None else None,
                 now))
            n = 1
        log.info("保养计划同步 %d 项", n)
        return n

    # ------------------------------------------------------------------
    async def run_daily(self) -> None:
        """T4 循环：每天 04:00 跑一次（简化：每 24h 一次，启动即跑）。

        附带 retention 清理（方案 §7）：state_history 原始 change 超过
        LIAM_RETENTION_DAYS 删除 + incremental_vacuum 回收。
        """
        while True:
            try:
                await self.sync_all()
            except LiApiError as e:
                log.warning("T4 云端同步失败（24h 后再试）: %s", e)
            except AuthExpiredError as exc:
                if exc.need_relogin:
                    raise
                log.warning("T4 鉴权暂时受限，24h 后再试")
            try:
                self._retention_sweep()
            except Exception as e:  # noqa: BLE001
                log.warning("retention 清理失败: %r", e)
            await asyncio.sleep(24 * 3600)

    def _retention_sweep(self) -> int:
        days = self.retention_days
        if days <= 0:
            return 0
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        cur = self.db.execute_ret(
            "DELETE FROM state_history WHERE ingested_at_ms < ?", (cutoff,))
        if cur and cur > 0:
            self.db.execute("PRAGMA incremental_vacuum")
            log.info("retention：清理 %d 行（> %d 天）", cur, days)
        return cur or 0


def _field(item: dict, *names: str) -> Any:
    for name in names:
        value = item.get(name)
        if value is not None and value != "":
            return value
    return None


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _iter_items(data: Any) -> list:
    """云端响应解包：list / {list} / {items} / {records} / {data:{...}}。"""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "items", "records", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
        inner = data.get("data")
        if isinstance(inner, (list, dict)) and inner is not data:
            return _iter_items(inner)
    return []


def _ms(v: Any) -> int | None:
    try:
        if v is None:
            return None
        n = float(v)
        if n > 1e12:       # ms
            return int(n)
        if n > 1e9:        # s
            return int(n * 1000)
        # "2026-11-19 12:00:00" 字符串 → epoch ms
    except (TypeError, ValueError):
        pass
    if isinstance(v, str):
        import datetime
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(datetime.datetime.strptime(v, fmt)
                           .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
            except ValueError:
                continue
    return None
