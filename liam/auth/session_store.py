"""SessionStore：凭据加密持久化（方案 §5.2）。

存 session_store 表（name → AES-GCM blob + 到期时间）。
★ 重启恢复 = M0 验收项：进程重启后凭据完整读回。
★ refresh_token 轮换即写库（与 LiApi.refresh_bearer 配合，事务内完成）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.db import Database
from ..core.secrets import SecretBox

# 命名约定（与 LiApi 字段一一对应）
K_BEARER = "bearer"
K_REFRESH = "refresh_token"
K_COOKIES = "cookies"          # dict → JSON 加密
K_DEVICE = "device_id"
K_HAC = "hac_key"
K_KEY_ID = "key_id"


class SessionStore:
    def __init__(self, db: Database, box: SecretBox) -> None:
        self._db = db
        self._box = box

    # ------------------------------------------------------------------
    def save(self, name: str, value: Any, expires_at_ms: int | None = None) -> None:
        blob = self._box.encrypt(
            value if isinstance(value, str) else json.dumps(value))
        now = int(time.time() * 1000)
        self._db.execute(
            "INSERT INTO session_store(name, value_enc, expires_at_ms, updated_at_ms) "
            "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "value_enc=excluded.value_enc, expires_at_ms=excluded.expires_at_ms, "
            "updated_at_ms=excluded.updated_at_ms",
            (name, blob, expires_at_ms, now))

    def load(self, name: str) -> Any:
        row = self._db.query_one(
            "SELECT value_enc FROM session_store WHERE name=?", (name,))
        if not row:
            return None
        raw = self._box.decrypt(row["value_enc"])
        if name == K_COOKIES:
            return json.loads(raw)
        return raw

    def load_with_expiry(self, name: str) -> tuple[Any, int | None]:
        row = self._db.query_one(
            "SELECT value_enc, expires_at_ms FROM session_store WHERE name=?",
            (name,))
        if not row:
            return None, None
        raw = self._box.decrypt(row["value_enc"])
        if name == K_COOKIES:
            raw = json.loads(raw)
        return raw, row["expires_at_ms"]

    # ------------------------------------------------------------------
    def snapshot_from_api(self, api) -> dict[str, Any]:
        """从 LiApi 实例抽取全部凭据（供整体落库）。"""
        return {
            K_BEARER: api.bearer,
            K_REFRESH: api.refresh_token,
            K_COOKIES: api.cookies,
            K_DEVICE: api.device_id,
        }

    def restore_into(self, api) -> bool:
        """恢复到 LiApi 实例；有 refresh_token 或 bearer 即视为可恢复。"""
        bearer = self.load(K_BEARER)
        refresh = self.load(K_REFRESH)
        cookies = self.load(K_COOKIES)
        device = self.load(K_DEVICE)
        if not (bearer or refresh):
            return False
        api.bearer = bearer or ""
        api.refresh_token = refresh or ""
        api.cookies = dict(cookies or {})
        if device:
            api.device_id = device
        return True

    def persist_all(self, api) -> None:
        """整体落库（bearer 过期时间未知则 NULL，由 401 反应式处理）。"""
        for k, v in self.snapshot_from_api(api).items():
            if v:
                self.save(k, v)
