from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class UserState:
    chat_id: int
    lang: str = "ru"
    status_message_id: int | None = None
    upload_path: str | None = None
    loaded_export: dict[str, Any] | None = None
    selected_target: str | None = None
    submitted_api_key: str | None = None
    busy: bool = False
    ui_mode: str = "home"
    last_prompt: str | None = None
    last_module_id: str | None = None
    last_result_full: str | None = None
    draft_result: str | None = None
    task_status: str = "idle"
    task_id: str | None = None
    test_chat_active: bool = False
    test_history: list[tuple[str, str]] | None = None


class SessionStore:
    def __init__(self) -> None:
        self._states: dict[int, UserState] = {}

    def get(self, chat_id: int) -> UserState:
        if chat_id not in self._states:
            self._states[chat_id] = UserState(chat_id=chat_id, test_history=[])
        return self._states[chat_id]

    def clear_export(self, chat_id: int) -> None:
        state = self.get(chat_id)
        state.upload_path = None
        state.loaded_export = None
        state.selected_target = None
        state.last_prompt = None
        state.last_result_full = None
        state.draft_result = None
        state.test_chat_active = False
        state.test_history = []
