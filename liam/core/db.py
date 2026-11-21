"""SQLite(WAL) 存储层：连接管理 + M0 schema。

设计依据：方案 §7（单文件、单写者、WAL、auto_vacuum=INCREMENTAL）。
M0 只建基础表；采集/会话/看板表（positions/charge_samples/…）M1/M2 增补，
统一走 migrations/0001_init.sql 之外的增量脚本 + user_version 版本管理。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
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

# M2 增补：会话三件套 + geofences（方案 §7）
_SCHEMA_M2 = """
CREATE TABLE IF NOT EXISTS drive_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER,
    start_lat REAL, start_lon REAL,
    end_lat REAL, end_lon REAL,
    start_geofence TEXT, end_geofence TEXT,
    distance_m REAL,                 -- Trip.Total accMileage 增量（m）
    duration_s INTEGER,
    avg_speed_kmh REAL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drive_time ON drive_sessions(vin, start_ms);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    drive_session_id INTEGER REFERENCES drive_sessions(id),
    ts_ms INTEGER NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL,
    spd REAL, dir REAL, alt REAL,
    valid INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pos_session ON positions(drive_session_id, ts_ms);

CREATE TABLE IF NOT EXISTS charge_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER,
    start_soc REAL, end_soc REAL,
    power_peak_kw REAL, power_avg_kw REAL,
    charge_type TEXT,                -- ac/dc（枪型枚举观察期标定）
    geofence TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_charge_time ON charge_sessions(vin, start_ms);

CREATE TABLE IF NOT EXISTS charge_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    charge_session_id INTEGER REFERENCES charge_sessions(id),
    ts_ms INTEGER NOT NULL,
    soc REAL, power_kw REAL,
    current REAL, voltage REAL
);
CREATE INDEX IF NOT EXISTS idx_charge_sample_session
    ON charge_samples(charge_session_id, ts_ms);

CREATE TABLE IF NOT EXISTS geofences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 100
);
"""

USER_VERSION = 3

# M3 增补：云端历史镜像（幂等 upsert，方案 §4.2）
_SCHEMA_M3 = """
CREATE TABLE IF NOT EXISTS travel_daily (
    vin TEXT NOT NULL,
    date TEXT NOT NULL,               -- YYYY-MM-DD（云端自然日）
    mileage_km REAL,
    energy_kwh REAL,
    raw_json TEXT,
    PRIMARY KEY (vin, date)
);

CREATE TABLE IF NOT EXISTS charge_records_cloud (
    vin TEXT NOT NULL,
    record_id TEXT PRIMARY KEY,       -- 云端记录 ID（幂等键）
    start_ms INTEGER, end_ms INTEGER,
    energy_kwh REAL, charging_type INTEGER,
    raw_json TEXT,
    synced_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crc_time ON charge_records_cloud(vin, start_ms);
"""

USER_VERSION = 4

# M6-info 增补：行车能耗推导（SOC 差 × 电池容量，方案 §8.4 efficiency）
_SCHEMA_M6 = """
ALTER TABLE drive_sessions ADD COLUMN energy_kwh REAL;
ALTER TABLE drive_sessions ADD COLUMN efficiency_kwh_100 REAL;
CREATE TABLE IF NOT EXISTS drive_soc_marks (
    drive_session_id INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    soc REAL NOT NULL,
    PRIMARY KEY (drive_session_id, ts_ms)
);
"""

USER_VERSION = 5

# M6-info-2 增补：OTA / 保养 / 幽灵功耗 / 常去地点
_SCHEMA_M6B = """
CREATE TABLE IF NOT EXISTS ota_updates (
    vin TEXT NOT NULL,
    version TEXT,
    status TEXT,
    raw_json TEXT,
    synced_at_ms INTEGER NOT NULL,
    PRIMARY KEY (vin)
);

CREATE TABLE IF NOT EXISTS maintenance_plan (
    vin TEXT NOT NULL,
    item_key TEXT NOT NULL,           -- 保养项（ha-lixiang 实测键：acfilter/
                                      -- coolfuild/enginelevel1/2/gearbrakeoil/
                                      -- sparkplug）
    raw_json TEXT,
    synced_at_ms INTEGER NOT NULL,
    PRIMARY KEY (vin, item_key)
);
"""

USER_VERSION = 6


class Database:
    """单连接 + 线程锁：单写者模型，M0 够用；M1 起采集在后台线程复用。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
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
            if v < 3:
                self._conn.executescript(_SCHEMA_M2)
            if v < 4:
                self._conn.executescript(_SCHEMA_M3)
            if v < 5:
                self._conn.executescript(_SCHEMA_M6)
            if v < 6:
                self._conn.executescript(_SCHEMA_M6B)
            if v < USER_VERSION:
                self._conn.execute(f"PRAGMA user_version = {USER_VERSION}")

    # --- 通用小助手（M0 阶段保持薄；M1 起按表封装 repository）---
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def insert(self, sql: str, params: tuple = ()) -> int:
        """单行插入（自动提交）→ 返回 lastrowid。

        ★ sessionizer 开会话用——勿用裸 _conn.execute（绕过 commit，
        进程退出未提交会回滚，实测踩坑：states 有 driving 但
        drive_sessions=0）。
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or 0

    def execute_ret(self, sql: str, params: tuple = ()) -> int:
        """执行并返回受影响行数（DELETE/UPDATE 统计用，自动提交）。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.rowcount

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """多行写入（单事务）：latest upsert + history insert 原子落库。"""
        with self.transaction():
            self._conn.executemany(sql, rows)

    @contextmanager
    def transaction(self):
        """同一连接的写入一起提交。嵌套保存点可独立回滚，最外层释放时提交。"""
        with self._lock:
            self._conn.execute("SAVEPOINT liam_write")
            try:
                yield self
                self._conn.execute("RELEASE liam_write")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK TO liam_write")
                    self._conn.execute("RELEASE liam_write")
                raise

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def backup_to(self, path: Path) -> None:
        """在线导出含 WAL 已提交内容的独立快照。"""
        with closing(sqlite3.connect(self._path)) as source, closing(sqlite3.connect(path)) as dest:
            source.backup(dest)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
