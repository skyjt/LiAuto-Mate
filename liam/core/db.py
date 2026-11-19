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

# M1 增补：状态表（方案 §7）
_SCHEMA_M1 = """
CREATE TABLE IF NOT EXISTS state_latest (
    vin TEXT NOT NULL,
    path TEXT NOT NULL,
    value_json TEXT NOT NULL,        -- 原始值（数字/字符串/JSON 字符串）JSON 编码
    ts_ms INTEGER,                    -- 车端 ts（VSS ms）
    source TEXT,
    ingested_at_ms INTEGER NOT NULL, -- 本地采集时间
    PRIMARY KEY (vin, path)
);

CREATE TABLE IF NOT EXISTS state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    path TEXT NOT NULL,
    value_json TEXT NOT NULL,
    ts_ms INTEGER,
    source TEXT,
    ingested_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_path_ts ON state_history(vin, path, ingested_at_ms);

-- 车辆状态时间线（TeslaMate states 同构，方案 §8.4 取经）
CREATE TABLE IF NOT EXISTS states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    state TEXT NOT NULL,             -- driving/charging/parked_awake/asleep/offline
    start_ms INTEGER NOT NULL,
    end_ms INTEGER                   -- NULL = 当前进行中
);
CREATE INDEX IF NOT EXISTS idx_states_time ON states(vin, start_ms);
"""

USER_VERSION = 2


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
            v = cur.fetchone()[0]
            if v < 1:
                self._conn.executescript(_SCHEMA_M0)
            if v < 2:
                self._conn.executescript(_SCHEMA_M1)
            if v < USER_VERSION:
                self._conn.execute(f"PRAGMA user_version = {USER_VERSION}")

    # --- 通用小助手（M0 阶段保持薄；M1 起按表封装 repository）---
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """多行写入（单事务）：latest upsert + history insert 原子落库。"""
        with self._lock, self._conn:
            self._conn.executemany(sql, rows)

    def transaction(self):
        """事务上下文：with db.transaction(): db.executemany(...)。

        RLock 可重入 + 嵌套 with 均指向同一 BEGIN...COMMIT —— 单写者模型
        下等价原子块（第二个 with 块不会重开事务）。
        """
        return _Txn(self)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Txn:
    """with db.transaction() 上下文（见 Database.transaction 注释）。"""

    __slots__ = ("_db",)

    def __init__(self, db: "Database") -> None:
        self._db = db

    def __enter__(self) -> "Database":
        self._db._lock.acquire()
        self._db._conn.execute("BEGIN")
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._db._conn.commit()
            else:
                self._db._conn.rollback()
        finally:
            self._db._lock.release()
