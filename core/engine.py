from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

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
                logger.info("engine_models_updated count=%s models=%s", len(clean), clean)

    def add_key(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                logger.info("engine_key_skip_duplicate key_prefix=%s", key[:8])
                return
            self._keys.append(key)
            self._states[key] = KeyState()
            logger.info("engine_key_added total_keys=%s key_prefix=%s", len(self._keys), key[:8])

    def _acquire_key(self) -> str:
        wait_loops = 0
        while True:
            with self._lock:
                now = time.time()
                for key in self._keys:
                    state = self._states[key]
                    if not state.in_use and state.unban_time <= now:
                        state.in_use = True
                        if wait_loops:
                            logger.info("engine_key_acquired_after_wait loops=%s key_prefix=%s", wait_loops, key[:8])
                        return key
            wait_loops += 1
            time.sleep(0.25)

    def _release_key(self, key: str, cooldown: float = 0) -> None:
        with self._lock:
            state = self._states[key]
            state.in_use = False
            state.unban_time = time.time() + cooldown
            logger.info("engine_key_released key_prefix=%s cooldown=%s", key[:8], cooldown)

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

        logger.info("engine_generate_start prompt_len=%s retries=%s models=%s key_count=%s", len(prompt), max_retries, self.models, len(self._keys))
        retries = 0
        while retries <= max_retries:
            key = self._acquire_key()
            client = genai.Client(api_key=key)
            model_error = None
            for model in self.models:
                try:
                    logger.info("engine_try_model key_prefix=%s model=%s attempt=%s", key[:8], model, retries + 1)
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=SAFE_CONFIG,
                    )
                    text = (response.text or "").strip()
                    if text:
                        self._release_key(key, cooldown=1)
                        logger.info("engine_generate_success model=%s text_len=%s", model, len(text))
                        return text
                except Exception as e:
                    model_error = str(e)
                    logger.warning("engine_model_failed model=%s key_prefix=%s err=%s", model, key[:8], model_error)
                    continue

            retries += 1
            cooldown = self._cooldown_for_error(model_error or "")
            logger.warning("engine_retry_scheduled attempt=%s cooldown=%s err=%s", retries, cooldown, model_error)
            self._release_key(key, cooldown=cooldown)

        logger.error("engine_generate_exhausted retries=%s", retries)
        raise RuntimeError("Gemini request failed after retries")


class AIEngine:
    def __init__(self, pool: GeminiPool) -> None:
        self.pool = pool

    def validate_key(self, key: str) -> bool:
        try:
            logger.info("engine_validate_key_start key_prefix=%s", key[:8])
            temp_pool = GeminiPool([key], models=self.pool.models)
            result = temp_pool.generate("Respond with exactly: ok")
            is_ok = "ok" in result.lower()
            logger.info("engine_validate_key_done key_prefix=%s valid=%s", key[:8], is_ok)
            return is_ok
        except Exception:
            logger.exception("engine_validate_key_failed key_prefix=%s", key[:8])
            return False

    def run_prompt_job(self, prompt: str) -> str:
        return self.pool.generate(prompt)

    def set_models(self, models: list[str]) -> None:
        self.pool.set_models(models)

    def get_models(self) -> list[str]:
        return self.pool.models
