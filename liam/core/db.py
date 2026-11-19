"""SQLite(WAL) 存储层：连接管理 + M0 schema。

设计依据：方案 §7（单文件、单写者、WAL、auto_vacuum=INCREMENTAL）。
M0 只建基础表；采集/会话/看板表（positions/charge_samples/…）M1/M2 增补，
统一走 migrations/0001_init.sql 之外的增量脚本 + user_version 版本管理。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA_M0 = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA auto_vacuum=INCREMENTAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 凭据保管（方案 §5.2）：值 AES-256-GCM 加密后存 blob，
-- 到期时间明文列便于看护任务查询。
CREATE TABLE IF NOT EXISTS session_store (
    name TEXT PRIMARY KEY,            -- 'bearer' | 'refresh_token' | 'cookies' | 'device_id' | 'hac_key' | 'key_id' | 'scope_vss'
    value_enc BLOB NOT NULL,          -- AES-256-GCM(nonce||ct)
    expires_at_ms INTEGER,            -- epoch ms；NULL=不过期/未知
    updated_at_ms INTEGER NOT NULL
);

-- 采集器每批次健康（方案 §7 collector_log；M1 起写入，M0 先建表）
CREATE TABLE IF NOT EXISTS collector_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    tier TEXT NOT NULL,               -- T0/T1/T1p/T1pp/T2/T3/T4
    state TEXT,                       -- driving/charging/parked_awake/asleep
    ok INTEGER NOT NULL,              -- 0/1
    latency_ms INTEGER,
    path_count INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_collector_ts ON collector_log(ts_ms);
"""

USER_VERSION = 1


class Database:
    """单连接 + 线程锁：单写者模型，M0 够用；M1 起采集在后台线程复用。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute("PRAGMA user_version")
            if cur.fetchone()[0] < USER_VERSION:
                self._conn.executescript(_SCHEMA_M0)
                self._conn.execute(f"PRAGMA user_version = {USER_VERSION}")

    # --- 通用小助手（M0 阶段保持薄；M1 起按表封装 repository）---
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
