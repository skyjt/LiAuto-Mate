"""生产采集器（M1 核心）：四态驱动分档调度（方案 §6.2/§6.3）。

调度表：
  driving      → T1 全量批 60s + T1loc 定位微批 10s
  charging     → T1 全量批 60s + T1chg 充电采样 30s
  parked_awake → T1 全量批 60s
  asleep       → T0 轻探测（仅 3 在线路径）——发现苏醒立即恢复
  T2 配置档   → 与状态无关，1h 一次
错误纪律：指数退避封顶 30min；AuthExpired → 停（keeper 已转 NEED_LOGIN）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..auth.li_api import AuthExpiredError, LiApi, LiApiError
from ..auth.session_keeper import SessionKeeper
from ..config import Settings
from ..core.db import Database
from .paths import T0_PROBE_PATHS, T1_LOC_PATHS, T2_PATHS, T1_PATHS, batched
from .sessionizer import Sessionizer
from .state_machine import (
    S_ASLEEP,
    S_CHARGING,
    S_DRIVING,
    S_PARKED_AWAKE,
    StateMachine,
    VehicleState,
)

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, api: LiApi, keeper: SessionKeeper, db: Database,
                 settings: Settings, vin: str) -> None:
        self.api = api
        self.keeper = keeper
        self.db = db
        self.settings = settings
        self.vin = vin
        self.sm = StateMachine(db, vin)
        self.sm.bootstrap_from_db()
        self.sessionizer = Sessionizer(db, vin, battery_kwh=settings.battery_kwh)
        self.t1_batches = batched(T1_PATHS)
        self.t2_batches = batched(T2_PATHS)
        self.loc_batch = T1_LOC_PATHS
        self.probe_batch = T0_PROBE_PATHS
        self._consec_fail = 0
        self._t2_last = 0.0
        self._prev_max_ts: int | None = None

    # ------------------------------------------------------------------
    # 批执行
    # ------------------------------------------------------------------

    async def _fetch(self, paths: list[str]) -> dict[str, dict]:
        tok = await self.keeper.get_scope_token()
        return await self.api.vss_get_batch(self.vin, paths, tok)

    async def _fetch_multi(self, batches: list[list[str]]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for b in batches:
            merged.update(await self._fetch(b))
        return merged

    # ------------------------------------------------------------------
    # 入库（latest upsert + change-only history + collector_log）
    # ------------------------------------------------------------------

    def _ingest(self, merged: dict[str, dict], tier: str) -> int:
        now = int(time.time() * 1000)
        rows_latest: list[tuple] = []
        rows_hist: list[tuple] = []
        for path, dp in merged.items():
            value_json = json.dumps(dp.get("value"), ensure_ascii=False)
            t = dp.get("ts")
            ts_ms = int(t) if isinstance(t, (int, float)) else None
            rows_latest.append((self.vin, path, value_json, ts_ms,
                                str(dp.get("source") or ""), now))
            old = self.db.query_one(
                "SELECT value_json FROM state_latest WHERE vin=? AND path=?",
                (self.vin, path))
            if old is None or old["value_json"] != value_json:
                rows_hist.append((self.vin, path, value_json, ts_ms,
                                  str(dp.get("source") or ""), now))
        with self.db.transaction():
            self.db.executemany(
                "INSERT INTO state_latest(vin,path,value_json,ts_ms,source,ingested_at_ms) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(vin,path) DO UPDATE SET "
                "value_json=excluded.value_json, ts_ms=excluded.ts_ms, "
                "source=excluded.source, ingested_at_ms=excluded.ingested_at_ms",
                rows_latest)
            if rows_hist:
                self.db.executemany(
                    "INSERT INTO state_history(vin,path,value_json,ts_ms,source,ingested_at_ms) "
                    "VALUES(?,?,?,?,?,?)", rows_hist)
        self.db.execute(
            "INSERT INTO collector_log(ts_ms,tier,state,ok,latency_ms,path_count,error) "
            "VALUES(?,?,?,?,?,?,?)",
            (now, tier, self.sm.state, 1, None, len(merged), None))
        return len(rows_hist)

    # ------------------------------------------------------------------
    # 各档任务
    # ------------------------------------------------------------------

    async def poll_full(self) -> str:
        """T1 全量批：取数 → 判定输入 → 状态机更新（含会话开闭钩子）→ 入库。"""
        t0 = time.monotonic()
        merged = await self._fetch_multi(self.t1_batches)
        vs = VehicleState(merged, self._prev_max_ts)
        prev_state = self.sm.state
        state = self.sm.update(vs)
        if state != prev_state:
            await self.sessionizer.on_state_change(prev_state, state, merged)
        elif ((state == S_DRIVING and self.sessionizer._drive_id is None)
              or (state == S_CHARGING and self.sessionizer._charge_id is None)):
            # ★ 重启自愈：状态从 DB 恢复（如 driving）但会话未开——补开
            #（之前部分丢失，会话起点记为当前时间——诚实处理）
            await self.sessionizer.on_state_change("", state, merged)
        changes = self._ingest(merged, "T1")
        # 更新 ts 水位
        for dp in merged.values():
            t = dp.get("ts")
            if isinstance(t, (int, float)) and (self._prev_max_ts or 0) < t:
                self._prev_max_ts = int(t)
        log.debug("T1 done state=%s changes=%d latency=%dms",
                  state, changes, (time.monotonic() - t0) * 1000)
        return state

    async def poll_loc(self) -> None:
        """T1loc 定位微批（10s，行驶态）：轨迹点入 positions。"""
        merged = await self._fetch(self.loc_batch)
        self._ingest(merged, "T1loc")
        self.sessionizer.add_position(merged)

    async def poll_charge_sample(self) -> None:
        """T1chg 充电采样（30s，充电态）：入 state + charge_samples。"""
        merged = await self._fetch(
            [p for p in T1_PATHS if "Battery" in p or "Charg" in p])
        self._ingest(merged, "T1chg")
        self.sessionizer.add_charge_sample(merged)

    async def poll_probe(self) -> str | None:
        """T0 休眠探测：在线→立即返回 awake 信号（恢复全量）。"""
        merged = await self._fetch(self.probe_batch)
        self._ingest(merged, "T0")
        vs = VehicleState(merged, self._prev_max_ts)
        if vs.online or vs.ts_fresh:
            return S_PARKED_AWAKE     # 苏醒信号
        return None

    async def poll_config(self) -> None:
        """T2 配置档（1h）：与状态无关。"""
        merged = await self._fetch_multi(self.t2_batches)
        self._ingest(merged, "T2")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        s = self.settings
        while True:
            try:
                state = await self._cycle_once()
                self._consec_fail = 0
            except AuthExpiredError:
                raise                      # keeper 转 NEED_LOGIN；serve 层处理
            except LiApiError as e:
                self._consec_fail += 1
                backoff = min(60 * (2 ** min(self._consec_fail, 5)), 1800)
                log.warning("采集失败 #%d，退避 %ds: %s",
                            self._consec_fail, backoff, e)
                await asyncio.sleep(backoff)
                continue
            # 各态延时
            if state == S_ASLEEP:
                await self._sleep_probe_loop()
            elif state == S_DRIVING:
                await self._drive_loop()
            elif state == S_CHARGING:
                await self._charge_loop()
            else:
                # parked_awake：T1 间隔；顺带 T2 到期检查
                if time.time() - self._t2_last >= s.poll_config:
                    try:
                        await self.poll_config()
                        self._t2_last = time.time()
                    except LiApiError as e:
                        log.warning("T2 失败（忽略，下轮再试）: %s", e)
                await asyncio.sleep(s.poll_realtime)

    async def _cycle_once(self) -> str:
        """一轮基础全量（所有状态共用入口）。"""
        return await self.poll_full()

    # --- 子循环（各自 sleep，状态变化即返回 run_forever 重判）---

    async def _sleep_probe_loop(self) -> None:
        s = self.settings
        while True:
            await asyncio.sleep(s.poll_sleep_probe)
            try:
                woke = await self.poll_probe()
                if woke:
                    log.info("车辆苏醒 → 恢复全量采集")
                    return
            except LiApiError as e:
                log.warning("T0 探测失败: %s", e)

    async def _drive_loop(self) -> None:
        s = self.settings
        next_full = time.monotonic()
        while True:
            await asyncio.sleep(s.poll_drive_loc)
            try:
                await self.poll_loc()
                now = time.monotonic()
                if now >= next_full:
                    state = await self.poll_full()   # 全量批兼做状态机更新
                    next_full = now + s.poll_realtime
                    if state != S_DRIVING:
                        return
            except LiApiError as e:
                log.warning("行驶采集失败: %s", e)

    async def _charge_loop(self) -> None:
        s = self.settings
        next_full = time.monotonic()
        while True:
            await asyncio.sleep(s.poll_charge)
            try:
                await self.poll_charge_sample()
                now = time.monotonic()
                if now >= next_full:
                    state = await self.poll_full()
                    next_full = now + s.poll_realtime
                    if state != S_CHARGING:
                        return
            except LiApiError as e:
                log.warning("充电采集失败: %s", e)
