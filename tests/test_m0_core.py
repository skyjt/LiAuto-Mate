"""SecretBox / SessionStore / signer 单元测试（纯本地，无网络）。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from liam.auth.session_store import K_BEARER, K_COOKIES, K_DEVICE, K_REFRESH, SessionStore
from liam.auth.signer import content_md5, sign_headers
from liam.core.db import Database
from liam.core.secrets import SecretBox


def test_secret_box_roundtrip(tmp_path):
    box = SecretBox.load_or_create(tmp_path)
    blob = box.encrypt("LISA-abc123")
    assert blob != b"LISA-abc123"
    assert box.decrypt(blob) == "LISA-abc123"
    # 同目录重载（重启恢复路径）
    box2 = SecretBox.load_or_create(tmp_path)
    assert box2.decrypt(blob) == "LISA-abc123"


def test_session_store_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    box = SecretBox.load_or_create(tmp_path)
    store = SessionStore(db, box)
    store.save(K_BEARER, "LISA-x")
    store.save(K_REFRESH, "HZ:y")
    store.save(K_COOKIES, {"sso_token": "tok", "ab_cookie": "1"})
    store.save(K_DEVICE, "dev123")
    assert store.load(K_BEARER) == "LISA-x"
    assert store.load(K_REFRESH) == "HZ:y"
    assert store.load(K_COOKIES) == {"sso_token": "tok", "ab_cookie": "1"}
    assert store.load(K_DEVICE) == "dev123"


def test_session_store_restore_restart(tmp_path):
    """M0 验收核心：『重启』后凭据完整恢复进新 LiApi 实例。"""
    from liam.auth.li_api import LiApi
    db = Database(tmp_path / "t.db")
    box = SecretBox.load_or_create(tmp_path)
    store = SessionStore(db, box)
    store.save(K_BEARER, "LISA-old")
    store.save(K_REFRESH, "HZ-rotated")
    store.save(K_COOKIES, {"sso_token": "s"})
    store.save(K_DEVICE, "deadauth")

    api2 = LiApi()                     # 模拟新进程
    assert store.restore_into(api2) is True
    assert api2.bearer == "LISA-old"
    assert api2.refresh_token == "HZ-rotated"
    assert api2.cookies == {"sso_token": "s"}
    assert api2.device_id == "deadauth"


def test_signer_sts_shape():
    """签名头结构：11 段配方关键位（与 spike 实测一致）。"""
    h = sign_headers("aa" * 32, "kid", "dev", "LISA-b", body='{"a":1}')
    assert h["X-CHJ-Env"] == "prod"
    assert h["X-CHJ-APP-Version"] == "8.25.4-10463"
    assert h["X-CHJ-Key"] == "kid"
    assert h["X-CHJ-Deviceid"] == "dev"
    assert h["X-CHJ-TOKEN"] == "LISA-b"
    assert h["Content-Language"] == "zh-Hans-CN"
    assert h["Accept"] == "*/*"
    assert h["Content-MD5"] == content_md5('{"a":1}')
    assert len(h["X-CHJ-Sign"]) > 0
    assert "X-CHJ-VIN" not in h       # 未传 vin 不带该头
    h2 = sign_headers("aa" * 32, "kid", "dev", "LISA-b", body="{}", vin="V1")
    assert h2["X-CHJ-VIN"] == "V1"


def test_key_suite_pure_functions():
    from liam.auth.key_suite import build_data_to_sign
    assert build_data_to_sign("r", "d", "k", "n", "t") == "r:d:k:n:t"


def test_write_helpers_respect_outer_rollback(tmp_path):
    with closing(Database(tmp_path / "atomic.db")) as db:
        sql = "INSERT INTO meta(key,value) VALUES(?,?)"
        db.execute(sql, ("before", "original"))
        with pytest.raises(sqlite3.IntegrityError), db.transaction():
            db.insert(sql, ("new", "one"))
            db.execute_ret("UPDATE meta SET value='changed' WHERE key='before'")
            db.executemany(sql, [("other", "two"), ("new", "duplicate")])
        assert [tuple(row) for row in db.query_all("SELECT * FROM meta")] == [("before", "original")]
        with db.transaction():
            db.execute(sql, ("outer", "keep"))
            with pytest.raises(sqlite3.IntegrityError), db.transaction():
                db.execute(sql, ("inner", "discard"))
                db.execute(sql, ("before", "duplicate"))
        assert db.query_one("SELECT value FROM meta WHERE key='outer'")["value"] == "keep"
        assert db.query_one("SELECT value FROM meta WHERE key='inner'") is None
