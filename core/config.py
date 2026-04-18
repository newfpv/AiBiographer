from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    tg_bot_token: str
    admin_id: int
    base_url: str
    local_data_dir: Path
    default_lang: str
    api_keys: list[str]


def _split_keys(raw: str) -> list[str]:
    return [k.strip() for k in raw.split(",") if k.strip()]


def load_settings() -> Settings:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    keys = _split_keys(os.getenv("API_KEYS", ""))
    if not token:
        raise RuntimeError("TG_BOT_TOKEN is required")

    data_dir = Path(os.getenv("LOCAL_DATA_DIR", "/app/data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        tg_bot_token=token,
        admin_id=int(os.getenv("ADMIN_ID", "0")),
        base_url=os.getenv("BASE_URL", "http://localhost:5000").rstrip("/"),
        local_data_dir=data_dir,
        default_lang=os.getenv("DEFAULT_LANG", "ru"),
        api_keys=keys,
    )
