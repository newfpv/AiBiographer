from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any
import zipfile
import json
from pathlib import Path
import re


def load_export(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as zf:
            json_name = next((n for n in zf.namelist() if n.endswith(".json") and not n.endswith("/.json")), None)
            if not json_name:
                raise ValueError("No JSON found in zip")
            with zf.open(json_name) as fp:
                return json.loads(fp.read().decode("utf-8"))
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)




def load_export_bytes(raw: bytes, filename_hint: str = "export.json") -> dict[str, Any]:
    if filename_hint.lower().endswith(".zip"):
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(raw), "r") as zf:
            json_name = next((n for n in zf.namelist() if n.endswith(".json") and not n.endswith("/.json")), None)
            if not json_name:
                raise ValueError("No JSON found in zip")
            with zf.open(json_name) as fp:
                return json.loads(fp.read().decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def all_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if isinstance(data.get("chats"), dict) and isinstance(data["chats"].get("list"), list):
        for chat in data["chats"]["list"]:
            chat_name = chat.get("name", "unknown_chat")
            for msg in chat.get("messages", []):
                msg["_chat"] = chat_name
                messages.append(msg)
    else:
        messages.extend(data.get("messages", []))
    return [m for m in messages if m.get("type") == "message"]


def flatten_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(item.get("text", "")))
        return "".join(out)
    return ""


def _only_target_texts(messages: list[dict[str, Any]], target: str | None) -> list[str]:
    own = [m for m in messages if not target or m.get("from") == target]
    texts = [flatten_text(m.get("text", "")).strip() for m in own]
    return [t for t in texts if t]


def collect_style_stats(messages: list[dict[str, Any]], target: str | None = None) -> dict[str, Any]:
    texts = _only_target_texts(messages, target)

    words = Counter()
    emoji_count = 0
    question_count = 0
    exclamation_count = 0
    brackets_laugh = 0
    caps_laugh = 0
    for text in texts:
        for token in text.lower().split():
            token = token.strip(".,!?;:\"'()[]{}")
            if len(token) >= 4:
                words[token] += 1
        emoji_count += sum(1 for ch in text if ord(ch) > 10000)
        question_count += text.count("?")
        exclamation_count += text.count("!")
        if re.search(r"\)+$", text):
            brackets_laugh += 1
        if re.search(r"[А-ЯA-Z]{4,}", text):
            caps_laugh += 1

    msg_count = max(len(texts), 1)
    return {
        "message_count": len(texts),
        "top_words": [w for w, _ in words.most_common(30)],
        "emoji_per_message": round(emoji_count / msg_count, 3),
        "question_per_message": round(question_count / msg_count, 3),
        "exclamation_per_message": round(exclamation_count / msg_count, 3),
        "laugh_with_brackets_ratio": round(brackets_laugh / msg_count, 3),
        "laugh_caps_ratio": round(caps_laugh / msg_count, 3),
    }


def summarize_timeline(messages: list[dict[str, Any]], target: str | None = None, limit: int = 140) -> list[str]:
    filtered = [m for m in messages if (not target or m.get("from") == target)]
    if not filtered:
        return []

    # Берем сообщения по всей длине истории, а не только начало.
    stride = max(1, len(filtered) // limit)
    picked = [filtered[i] for i in range(0, len(filtered), stride)][:limit]

    out: list[str] = []
    for m in picked:
        text = flatten_text(m.get("text", "")).replace("\n", " ").strip()
        if not text:
            continue
        date_raw = m.get("date")
        try:
            dt = datetime.fromisoformat(date_raw)
            date_norm = dt.strftime("%Y-%m-%d")
        except Exception:
            date_norm = "unknown-date"
        chat_name = m.get("_chat", "chat")
        out.append(f"[{date_norm}] ({chat_name}) {m.get('from', 'unknown')}: {text[:220]}")

    return out


def detect_candidates(messages: list[dict[str, Any]], limit: int = 12) -> list[str]:
    counter = Counter(m.get("from", "unknown") for m in messages if m.get("from"))
    return [name for name, _ in counter.most_common(limit)]


def build_profile_hints(messages: list[dict[str, Any]], target: str | None = None) -> dict[str, Any]:
    texts = _only_target_texts(messages, target)
    merged = "\n".join(texts[:1500])

    forbidden_words = [w for w in ["пас", "старик", "ты гонишь", "дарова", "ку", "привет"] if w in merged.lower()]
    rough_words = [w for w in ["бля", "пиздец", "аху", "нах"] if w in merged.lower()]

    ping_replies = []
    for text in texts[:2000]:
        low = text.lower().strip()
        if low in {"а?", "м?", "чо?", "че?"}:
            ping_replies.append(text)

    return {
        "forbidden_candidates": forbidden_words,
        "mat_words_seen": rough_words,
        "short_ping_replies": list(dict.fromkeys(ping_replies))[:10],
    }
