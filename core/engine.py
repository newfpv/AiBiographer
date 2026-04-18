from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from google import genai
from google.genai import types


DEFAULT_FREE_MODELS = [
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash-lite",
]

SAFE_CONFIG = types.GenerateContentConfig(
    safety_settings=[
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]
)


@dataclass
class KeyState:
    in_use: bool = False
    unban_time: float = 0


class GeminiPool:
    def __init__(self, keys: list[str], models: list[str] | None = None) -> None:
        self._keys = keys[:]
        self._states = {k: KeyState() for k in self._keys}
        self._lock = threading.Lock()
        self._models = models[:] if models else DEFAULT_FREE_MODELS[:]

    @property
    def models(self) -> list[str]:
        return self._models[:]

    def set_models(self, models: list[str]) -> None:
        with self._lock:
            clean = [m.strip() for m in models if m.strip()]
            if clean:
                self._models = clean

    def add_key(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                return
            self._keys.append(key)
            self._states[key] = KeyState()

    def _acquire_key(self) -> str:
        while True:
            with self._lock:
                now = time.time()
                for key in self._keys:
                    state = self._states[key]
                    if not state.in_use and state.unban_time <= now:
                        state.in_use = True
                        return key
            time.sleep(0.25)

    def _release_key(self, key: str, cooldown: float = 0) -> None:
        with self._lock:
            state = self._states[key]
            state.in_use = False
            state.unban_time = time.time() + cooldown

    @staticmethod
    def _cooldown_for_error(err: str) -> float:
        e = err.lower()
        if "429" in e or "resource_exhausted" in e:
            if "daily" in e or "quota" in e:
                return 300
            return 20
        if "503" in e or "500" in e:
            return 15
        if "timeout" in e:
            return 8
        return 5

    def generate(self, prompt: str, max_retries: int = 6) -> str:
        if not self._keys:
            raise RuntimeError("No Gemini API keys available")

        retries = 0
        while retries <= max_retries:
            key = self._acquire_key()
            client = genai.Client(api_key=key)
            model_error = None
            for model in self.models:
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=SAFE_CONFIG,
                    )
                    text = (response.text or "").strip()
                    if text:
                        self._release_key(key, cooldown=1)
                        return text
                except Exception as e:
                    model_error = str(e)
                    continue

            retries += 1
            cooldown = self._cooldown_for_error(model_error or "")
            self._release_key(key, cooldown=cooldown)

        raise RuntimeError("Gemini request failed after retries")


class AIEngine:
    def __init__(self, pool: GeminiPool) -> None:
        self.pool = pool

    def validate_key(self, key: str) -> bool:
        try:
            temp_pool = GeminiPool([key], models=self.pool.models)
            result = temp_pool.generate("Respond with exactly: ok")
            return "ok" in result.lower()
        except Exception:
            return False

    def run_prompt_job(self, prompt: str) -> str:
        return self.pool.generate(prompt)

    def set_models(self, models: list[str]) -> None:
        self.pool.set_models(models)

    def get_models(self) -> list[str]:
        return self.pool.models
