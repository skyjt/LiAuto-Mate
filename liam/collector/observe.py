"""观察采集器（M1 观察期专用）：60s 固定间隔直采全量路径入库。

目的（方案 §13.2 纪律）：先观察本车真实的休眠/苏醒/充电/行驶模式，
48h 后用数据定四态状态机的滞回参数——不拍脑袋。
与生产采集器的区别：无状态机、无分档，固定 60s 全量批（对齐 ha-lixiang
社区验证强度），失败退避但不停。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..auth.li_api import AuthExpiredError, LiApi, LiApiError
from ..auth.session_keeper import SessionKeeper
from ..core.db import Database
from .paths import OBSERVE_PATHS, T0_PROBE_PATHS, batched

log = logging.getLogger(__name__)

OBSERVE_INTERVAL = 60


class ObserveCollector:
    def __init__(self, api: LiApi, keeper: SessionKeeper, db: Database,
                 vin: str) -> None:
        self.api = api
        self.keeper = keeper
        self.db = db
        self.vin = vin
        self.batches = batched(OBSERVE_PATHS)   # 每批 ≤50
        self.consecutive_failures = 0
        self.total_polls = 0

    # ------------------------------------------------------------------
    async def poll_once(self) -> dict[str, Any]:
        """一批全量轮询（多批串行）→ 入库 → 返回本轮摘要。"""
        t0 = time.monotonic()
        merged: dict[str, dict] = {}
        err = ""
        scope_tok = await self.keeper.get_scope_token()
        for batch in self.batches:
            try:
                result = await self.api.vss_get_batch(self.vin, batch, scope_tok)
                merged.update(result)
            except AuthExpiredError:
                raise
            except LiApiError as e:
                err = str(e)[:200]
                log.warning("批失败（%d 路径）: %s", len(batch), e)
        latency_ms = int((time.monotonic() - t0) * 1000)
        self._ingest(merged, latency_ms, err)
        self.total_polls += 1
        if err and not merged:
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
        return {"paths": len(merged), "latency_ms": latency_ms, "error": err}

    def _ingest(self, merged: dict[str, dict], latency_ms: int, err: str) -> None:
        """写 state_latest（upsert）+ change-only state_history + collector_log。"""
        now = int(time.time() * 1000)
        rows_latest: list[tuple] = []
        rows_hist: list[tuple] = []
        for path, dp in merged.items():
            raw = dp.get("value")
            value_json = json.dumps(raw, ensure_ascii=False)
            # dp.ts 可能是 ms 整数/浮点或 tsFormat 字符串——原样存数字
            ts = dp.get("ts") or dp.get("tsFormat")
            ts_ms = None
            if isinstance(ts, (int, float)):
                ts_ms = int(ts)
            elif isinstance(ts, str) and ts[:13].isdigit():
                ts_ms = int(ts[:13])   # tsFormat "1793838703076..." 截 ms
            rows_latest.append((self.vin, path, value_json, ts_ms,
                                str(dp.get("source") or ""), now))
            # change-only：与 latest 旧值比较
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
            (now, "T1", "observe", 1 if merged else 0, latency_ms,
             len(merged), err or None))

    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        """观察期主循环：60s 固定 + 失败指数退避（封顶 30min）。"""
        delay = OBSERVE_INTERVAL
        while True:
            try:
                summary = await self.poll_once()
                delay = OBSERVE_INTERVAL
                log.info("observe poll #%d: %s", self.total_polls, summary)
            except AuthExpiredError as e:
                log.error("会话失效，观察停止（需重登）: %s", e)
                return
            except Exception as e:  # noqa: BLE001
                self.consecutive_failures += 1
                backoff = min(OBSERVE_INTERVAL * (2 ** min(self.consecutive_failures, 6)),
                              1800)
                delay = backoff
                log.warning("observe 轮询异常 #%d，退避 %ds: %r",
                            self.consecutive_failures, backoff, e)
            await asyncio.sleep(delay)
