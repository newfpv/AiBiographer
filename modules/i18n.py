from __future__ import annotations

from pathlib import Path
import i18n


def setup_i18n(base_path: Path, default_locale: str = "ru") -> None:
    i18n.set("filename_format", "{locale}.{format}")
    i18n.set("file_format", "json")
    i18n.set("skip_locale_root_data", True)
    i18n.load_path.append(str(base_path / "locales"))
    i18n.set("fallback", default_locale)
    i18n.set("locale", default_locale)


def t(key: str, locale: str = "ru", **kwargs: str) -> str:
    i18n.set("locale", locale)
    return i18n.t(key, **kwargs)
