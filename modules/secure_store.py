from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet


class SecureBlobStore:
    def __init__(self, root: Path, secret: str | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        key = self._resolve_key(secret)
        self._fernet = Fernet(key)

    @staticmethod
    def _resolve_key(secret: str | None) -> bytes:
        raw = (secret or os.getenv("SECURE_BLOB_KEY", "")).strip()
        if raw:
            # Пользователь может дать произвольный секрет, приводим к 32-байтному ключу Fernet.
            padded = (raw.encode("utf-8") + b"0" * 32)[:32]
            return base64.urlsafe_b64encode(padded)
        return Fernet.generate_key()

    def save_encrypted(self, blob_id: str, data: bytes) -> Path:
        payload = self._fernet.encrypt(data)
        path = self.root / f"{blob_id}.enc"
        path.write_bytes(payload)
        return path

    def read_decrypted(self, blob_id: str) -> bytes | None:
        path = self.root / f"{blob_id}.enc"
        if not path.exists():
            return None
        payload = path.read_bytes()
        return self._fernet.decrypt(payload)

    def delete_blob(self, blob_id: str) -> None:
        path = self.root / f"{blob_id}.enc"
        if path.exists():
            path.unlink(missing_ok=True)
