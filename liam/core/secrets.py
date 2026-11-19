"""凭据加密保管：AES-256-GCM（方案 §5.2 SessionStore 后端）。

密钥来源：data_dir/.secret.key（首次生成 32B 随机；权限 0600）。
该文件与 SQLite 同卷备份/迁移——拷卷即完整搬家。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretBox:
    """单密钥 AES-GCM 封装：encrypt() → nonce(12B) || ct+tag。"""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"secret key must be 32 bytes, got {len(key)}")
        self._aes = AESGCM(key)

    @classmethod
    def load_or_create(cls, data_dir: Path) -> "SecretBox":
        key_file = data_dir / ".secret.key"
        if key_file.exists():
            key = key_file.read_bytes()
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            # 0600：防同机其他用户读取；仅在未设置 umask 环境兜底
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
        return cls(key)

    def encrypt(self, plaintext: str | bytes) -> bytes:
        data = plaintext.encode() if isinstance(plaintext, str) else plaintext
        nonce = secrets.token_bytes(12)
        return nonce + self._aes.encrypt(nonce, data, None)

    def decrypt(self, blob: bytes) -> str:
        return self._aes.decrypt(blob[:12], blob[12:], None).decode()
