from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class BotModule:
    module_id: str
    title: str
    description: str
    requires_export: bool
    run: Callable[..., str]


class ModuleRegistry:
    def __init__(self) -> None:
        self._items: dict[str, BotModule] = {}

    def register(self, module: BotModule) -> None:
        self._items[module.module_id] = module

    def get(self, module_id: str) -> BotModule | None:
        return self._items.get(module_id)

    def list_items(self) -> list[BotModule]:
        return list(self._items.values())
