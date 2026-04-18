from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging

from modules.prompt_pipeline import build_generator_prompt
from modules.telegram_data import (
    all_messages,
    build_profile_hints,
    collect_style_stats,
    detect_candidates,
    summarize_timeline,
)

logger = logging.getLogger(__name__)


def _batch(lines: list[str], batch_size: int) -> list[list[str]]:
    return [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]


def _compress_batch(chunk: list[str], per_line_limit: int = 160) -> list[str]:
    out = []
    for line in chunk:
        out.append(line[:per_line_limit])
    return out


def _normalize_prompt(text: str) -> str:
    # Легкая дедупликация повторяющихся строк/правил
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        key = line.strip().lower()
        if not key:
            out.append(line)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def run_prompt_module(engine, loaded_export: dict, target_name: str) -> str:
    logger.info("prompt_module_start target=%s", target_name)
    messages = all_messages(loaded_export)
    style = collect_style_stats(messages, target_name)

    timeline = summarize_timeline(messages, target_name, limit=300)
    # [19] batching + parallel preprocessing
    batched = _batch(timeline, 30)
    compressed: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for part in ex.map(_compress_batch, batched):
            compressed.extend(part)

    # [20] size limit before LLM
    char_budget = 30000
    payload_lines = []
    used = 0
    for line in compressed:
        if used + len(line) > char_budget:
            break
        payload_lines.append(line)
        used += len(line)

    contacts = detect_candidates(messages, limit=20)
    hints = build_profile_hints(messages, target_name)
    llm_input = build_generator_prompt(target_name, style, payload_lines, contacts, hints)
    logger.info("prompt_module_llm_input_ready target=%s payload_lines=%s llm_chars=%s", target_name, len(payload_lines), len(llm_input))
    raw = engine.run_prompt_job(llm_input)
    final = _normalize_prompt(raw)
    logger.info("prompt_module_done target=%s raw_len=%s final_len=%s", target_name, len(raw), len(final))
    return final


def run_biography_module(engine, loaded_export: dict, target_name: str) -> str:
    logger.info("bio_module_start target=%s", target_name)
    messages = all_messages(loaded_export)
    timeline = summarize_timeline(messages, target_name, limit=300)

    char_budget = 32000
    payload = []
    size = 0
    for line in timeline:
        if size + len(line) > char_budget:
            break
        payload.append(line)
        size += len(line)

    prompt = (
        "Сделай биографию человека на русском языке на основе хронологии сообщений. "
        "Пиши структурно: Кто это, Характер, Отношения, Работа/занятия, Хронология ключевых событий. "
        "Не выдумывай, где данных мало — пиши неопределенно.\n\n"
        f"Цель: {target_name}\n\nХронология:\n" + "\n".join(payload)
    )
    logger.info("bio_module_llm_input_ready target=%s payload_lines=%s llm_chars=%s", target_name, len(payload), len(prompt))
    result = engine.run_prompt_job(prompt)
    logger.info("bio_module_done target=%s result_len=%s", target_name, len(result))
    return result
