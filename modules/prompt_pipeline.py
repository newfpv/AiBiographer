from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

@dataclass
class PromptArtifacts:
    system_prompt: str
    debug_payload: dict[str, Any]


GENERATOR_INSTRUCTION = """
Ты создаешь финальный SYSTEM PROMPT для модуля AI Twin автоответчика.
Нужно мимикрировать под реального человека максимально точно.

Верни ТОЛЬКО готовый промпт (без пояснений), в таком каркасе и стиле:

ТЫ <ИМЯ>, <ВОЗРАСТ если есть>, <ГОРОД если есть>. <РОЛИ/КТО ОН>.

[ПРАВИЛА]:
ФОРМАТ: ...
ЗАПРЕТЫ: ...
ОТКАЗЫ (ТОЛЬКО НА ПРЕДЛОЖЕНИЯ): ...
СМЕХ: ...
ТОН: ...
ФИЛЬТР И ПАМЯТЬ: ...

[БАЗА]: ...

[ПРИМЕРЫ]:
С: ...
Т: ...

[СТОП-КРАН]: ...

КРИТИЧЕСКИЕ ТРЕБОВАНИЯ:
1) Формат должен быть совместим с автоответчиком: короткие ответы, учёт истории, теги вроде [LIKE].
2) Не выдумывай факты: если данных мало — пиши нейтрально и осторожно.
3) Никаких длинных абзацев, только практические правила поведения.
4) Сохраняй живой сленг, если он подтверждается данными.
5) Добавь точные запреты слов/паттернов только если они реально встречаются в профиле.
6) В [ПРИМЕРЫ] дай 6-10 коротких мини-диалогов в стиле пользователя.
""".strip()


def _compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def build_generator_prompt(
    target_name: str,
    style_stats: dict[str, Any],
    timeline_snippets: list[str],
    candidate_contacts: list[str],
    profile_hints: dict[str, Any],
) -> str:
    payload = {
        "target_name": target_name,
        "style_stats": style_stats,
        "timeline_snippets": timeline_snippets,
        "candidate_contacts": candidate_contacts,
        "profile_hints": profile_hints,
    }

    prompt = (
        f"{GENERATOR_INSTRUCTION}\n\n"
        f"ВХОДНЫЕ ДАННЫЕ JSON:\n{_compact_json(payload)}\n\n"
        "Сначала извлеки подтвержденные факты, потом выдай финальный prompt строго по каркасу."
    )
    logger.info(
        "prompt_generator_built target=%s timeline=%s contacts=%s chars=%s",
        target_name,
        len(timeline_snippets),
        len(candidate_contacts),
        len(prompt),
    )
    return prompt
