from __future__ import annotations

import threading
import logging
import time
import telebot
from telebot import apihelper
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import Settings
from core.engine import AIEngine, DEFAULT_FREE_MODELS, GeminiPool
from modules.access_store import AccessStore
from modules.builtin_modules import run_biography_module, run_prompt_module
from modules.i18n import t
from modules.module_registry import BotModule, ModuleRegistry
from modules.secure_store import SecureBlobStore
from modules.task_queue import QueueTask, TaskQueue
from modules.telegram_data import all_messages, detect_candidates, load_export_bytes

logger = logging.getLogger(__name__)


class UserState:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.lang = "ru"
        self.status_message_id: int | None = None
        self.upload_path: str | None = None
        self.loaded_export: dict | None = None
        self.selected_target: str | None = None
        self.submitted_api_key: str | None = None
        self.busy = False
        self.ui_mode = "home"
        self.last_prompt: str | None = None
        self.last_module_id: str | None = None
        self.last_result_full: str | None = None
        self.draft_result: str | None = None
        self.task_status = "idle"
        self.task_id: str | None = None
        self.test_chat_active = False
        self.test_history: list[tuple[str, str]] = []


class SessionStore:
    def __init__(self) -> None:
        self._states: dict[int, UserState] = {}

    def get(self, chat_id: int) -> UserState:
        if chat_id not in self._states:
            self._states[chat_id] = UserState(chat_id)
            logger.info("state_created chat_id=%s", chat_id)
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
        logger.info("state_export_cleared chat_id=%s", chat_id)


class TwinBotApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = telebot.TeleBot(settings.tg_bot_token)
        self.sessions = SessionStore()
        self.access = AccessStore(settings.local_data_dir / "access_keys.sqlite")
        self.engine = AIEngine(GeminiPool(settings.api_keys, models=DEFAULT_FREE_MODELS))
        self.secure_store = SecureBlobStore(settings.local_data_dir / "secure_uploads")
        self.registry = ModuleRegistry()
        self.task_queue = TaskQueue()
        self._register_builtin_modules()
        self._bootstrap_saved_keys_into_pool()
        self._register_handlers()
        logger.info("twin_bot_initialized admin_id=%s default_lang=%s", settings.admin_id, settings.default_lang)

    def _bootstrap_saved_keys_into_pool(self) -> None:
        loaded = 0
        for _, key in self.access.list_active_keys():
            self.engine.pool.add_key(key)
            loaded += 1
        logger.info("bootstrap_saved_keys loaded=%s", loaded)

    def _register_builtin_modules(self) -> None:
        self.registry.register(
            BotModule(
                module_id="prompt",
                title="🧬 AI Twin Prompt",
                description="Собрать финальный prompt для автоответчика",
                requires_export=True,
                run=lambda engine, exp, target: run_prompt_module(engine, exp, target),
            )
        )
        self.registry.register(
            BotModule(
                module_id="bio",
                title="📖 Биография",
                description="Собрать краткую биографию по переписке",
                requires_export=True,
                run=lambda engine, exp, target: run_biography_module(engine, exp, target),
            )
        )

    def _register_handlers(self) -> None:
        @self.bot.message_handler(commands=["start", "menu"])
        def start(message):
            logger.info("event_start chat_id=%s", message.chat.id)
            state = self.sessions.get(message.chat.id)
            state.lang = self.settings.default_lang
            state.ui_mode = "await_key" if not self._has_access(message.chat.id) else "home"
            self.render_home(
                message.chat.id,
                notice="/start: 1) введите ключ 2) загрузите чат 3) выберите цель 4) выберите модуль",
            )

        @self.bot.message_handler(content_types=["document"])
        def handle_doc(message):
            logger.info("event_document chat_id=%s filename=%s", message.chat.id, message.document.file_name)
            state = self.sessions.get(message.chat.id)
            ext = (message.document.file_name or "").lower()
            if not (ext.endswith(".json") or ext.endswith(".zip")):
                self.render_home(message.chat.id, notice="❌ Нужен .json или .zip")
                return
            file_info = self.bot.get_file(message.document.file_id)
            raw = self.bot.download_file(file_info.file_path)
            state.task_status = "upload_parsing"
            self.render_home(message.chat.id, notice="⏳ Парсинг файла...")
            try:
                data = load_export_bytes(raw, filename_hint=ext)
            except Exception:
                logger.exception("upload_parse_failed chat_id=%s", message.chat.id)
                self.render_home(message.chat.id, notice=t("ui.upload_fail", locale=state.lang))
                return
            state.loaded_export = data
            state.upload_path = None
            state.task_status = "idle"
            self.render_home(message.chat.id, notice=t("ui.upload_ok", locale=state.lang))

        @self.bot.message_handler(func=lambda m: True)
        def text_router(message):
            state = self.sessions.get(message.chat.id)
            txt = (message.text or "").strip()
            logger.info("event_text chat_id=%s mode=%s len=%s", message.chat.id, state.ui_mode, len(txt))
            if not txt:
                return

            if state.test_chat_active:
                self._process_test_chat_message(message.chat.id, txt)
                return

            if state.ui_mode == "await_key":
                self._handle_key_input(message.chat.id, txt)
            elif state.ui_mode == "await_target":
                state.selected_target = txt
                state.ui_mode = "home"
                self.render_home(message.chat.id)
            elif state.ui_mode == "await_models" and message.chat.id == self.settings.admin_id:
                models = [x.strip() for x in txt.split(",") if x.strip()]
                self.engine.set_models(models)
                state.ui_mode = "home"
                self.render_home(message.chat.id, notice="✅ Список моделей обновлен")

        @self.bot.callback_query_handler(func=lambda c: True)
        def on_callback(call):
            chat_id = call.message.chat.id
            state = self.sessions.get(chat_id)
            logger.info("event_callback chat_id=%s data=%s mode=%s", chat_id, call.data, state.ui_mode)

            try:
                if call.data == "refresh":
                    state.ui_mode = "home"
                    self.render_home(chat_id)
                elif call.data == "add_key":
                    state.ui_mode = "await_key"
                    self.render_home(chat_id, notice=t("ui.ask_key", locale=state.lang))
                elif call.data == "revoke_key":
                    self.access.revoke_key(chat_id)
                    state.ui_mode = "await_key"
                    state.loaded_export = None
                    self.render_home(chat_id, notice=t("ui.key_revoked", locale=state.lang))
                elif call.data == "pick_target":
                    self._show_target_picker(chat_id)
                elif call.data.startswith("target:"):
                    state.selected_target = call.data.split(":", 1)[1]
                    state.ui_mode = "home"
                    self.render_home(chat_id)
                elif call.data == "target_custom":
                    state.ui_mode = "await_target"
                    self.render_home(chat_id, notice=t("ui.ask_target", locale=state.lang))
                elif call.data == "target_top1":
                    self._select_top_target(chat_id)
                elif call.data == "target_me":
                    self._select_top_target(chat_id)
                elif call.data.startswith("module:"):
                    self._start_module(chat_id, call.data.split(":", 1)[1])
                elif call.data.startswith("module_info:"):
                    mid = call.data.split(":", 1)[1]
                    module = self.registry.get(mid)
                    self.render_home(chat_id, notice=f"ℹ️ {module.title}: {module.description}" if module else "Модуль не найден")
                elif call.data == "rerun_last":
                    if state.last_module_id:
                        self._start_module(chat_id, state.last_module_id)
                    else:
                        self.render_home(chat_id, notice="Нет предыдущего запуска")
                elif call.data == "show_draft":
                    self._send_short_preview(chat_id)
                elif call.data == "approve_final":
                    self._send_full_result(chat_id)
                elif call.data == "start_test_prompt":
                    self._start_test_prompt(chat_id)
                elif call.data == "stop_test_prompt":
                    state.test_chat_active = False
                    self.render_home(chat_id, notice="🛑 Тест промпта остановлен")
                elif call.data == "lang_ru":
                    state.lang = "ru"
                    self.render_home(chat_id, notice=t("ui.language_set", locale=state.lang))
                elif call.data == "open_web":
                    self.bot.answer_callback_query(call.id, url=f"{self.settings.base_url}/bots/upload?user_id={chat_id}")
                    return
                elif call.data == "admin_panel" and chat_id == self.settings.admin_id:
                    self._open_admin_panel(chat_id)
                elif call.data == "admin_keys" and chat_id == self.settings.admin_id:
                    self._show_all_keys(chat_id)
                elif call.data == "admin_models" and chat_id == self.settings.admin_id:
                    self._show_models(chat_id)
                elif call.data == "admin_set_models" and chat_id == self.settings.admin_id:
                    state.ui_mode = "await_models"
                    self.render_home(chat_id, notice="Введите модели через запятую")
                elif call.data == "admin_set_free_models" and chat_id == self.settings.admin_id:
                    self.engine.set_models(DEFAULT_FREE_MODELS)
                    self.render_home(chat_id, notice="✅ Установлены бесплатные модели")
            except Exception as e:
                logger.exception("callback_failed chat_id=%s data=%s", chat_id, call.data)
                self.render_home(chat_id, notice=f"❌ Ошибка кнопки: {e}")
            finally:
                try:
                    self.bot.answer_callback_query(call.id)
                except Exception:
                    pass

    def _try_attach_web_upload(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        blob_id = f"upload_{chat_id}"
        try:
            raw = self.secure_store.read_decrypted(blob_id)
            if raw:
                logger.info("web_blob_attached chat_id=%s blob_id=%s size=%s", chat_id, blob_id, len(raw))
                state.task_status = "upload_parsing"
                state.loaded_export = load_export_bytes(raw, filename_hint="upload.json")
                self.secure_store.delete_blob(blob_id)
                state.task_status = "idle"
        except Exception:
            logger.exception("web_blob_attach_failed chat_id=%s blob_id=%s", chat_id, blob_id)

    def _has_access(self, chat_id: int) -> bool:
        if chat_id == self.settings.admin_id:
            return True
        return self.access.get_active_key(chat_id) is not None

    def _handle_key_input(self, chat_id: int, key: str) -> None:
        state = self.sessions.get(chat_id)
        logger.info("key_submit chat_id=%s key_prefix=%s", chat_id, key[:8])
        if self.engine.validate_key(key):
            self.access.upsert_key(chat_id, key, is_active=True)
            self.engine.pool.add_key(key)
            state.submitted_api_key = key
            state.ui_mode = "home"
            self.render_home(chat_id, notice=t("ui.key_saved", locale=state.lang))
        else:
            logger.warning("key_invalid chat_id=%s", chat_id)
            self.render_home(chat_id, notice=t("ui.key_bad", locale=state.lang))

    def _show_target_picker(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.loaded_export:
            logger.info("target_picker_blocked_no_export chat_id=%s", chat_id)
            self.render_home(chat_id, notice=t("ui.need_export", locale=state.lang))
            return
        names = detect_candidates(all_messages(state.loaded_export))
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(InlineKeyboardButton("👤 Топ-1 собеседник", callback_data="target_top1"))
        markup.add(InlineKeyboardButton("🙋 Я сам (авто)", callback_data="target_me"))
        for name in names[:8]:
            markup.add(InlineKeyboardButton(name[:28], callback_data=f"target:{name[:60]}"))
        markup.add(InlineKeyboardButton("✍️ Ввести вручную", callback_data="target_custom"))
        self._edit_or_send(chat_id, "Выбери цель для анализа:", markup)
        logger.info("target_picker_opened chat_id=%s candidate_count=%s", chat_id, len(names))

    def _select_top_target(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        names = detect_candidates(all_messages(state.loaded_export or {}), limit=1)
        if names:
            state.selected_target = names[0]
            logger.info("target_auto_selected chat_id=%s target=%s", chat_id, names[0])
            self.render_home(chat_id, notice=f"Цель выбрана: {names[0]}")
        else:
            logger.warning("target_auto_select_failed chat_id=%s", chat_id)
            self.render_home(chat_id, notice="Не удалось авто-выбрать цель")

    def _start_module(self, chat_id: int, module_id: str) -> None:
        state = self.sessions.get(chat_id)
        module = self.registry.get(module_id)
        if not module:
            logger.warning("module_missing chat_id=%s module=%s", chat_id, module_id)
            self.render_home(chat_id, notice="❌ Модуль не найден")
            return
        if not self._has_access(chat_id):
            self.render_home(chat_id, notice=t("ui.status_no_access", locale=state.lang))
            return
        if module.requires_export and not state.loaded_export:
            self.render_home(chat_id, notice=t("ui.need_export", locale=state.lang))
            return
        if not state.selected_target:
            self.render_home(chat_id, notice=t("ui.need_target", locale=state.lang))
            return

        task_id = f"{chat_id}:{module_id}:{state.selected_target}"
        state.task_status = "queued"
        state.busy = True
        state.task_id = task_id
        state.last_module_id = module_id
        self.render_home(chat_id, notice=f"⏳ Поставлено в очередь: {module.title}")
        logger.info("task_queued chat_id=%s task_id=%s module=%s target=%s", chat_id, task_id, module_id, state.selected_target)

        def run_task() -> str:
            state.task_status = "running"
            logger.info("task_started chat_id=%s task_id=%s", chat_id, task_id)
            return module.run(self.engine, state.loaded_export, state.selected_target)

        def on_success(result: str) -> None:
            st = self.sessions.get(chat_id)
            st.busy = False
            st.task_status = "done"
            st.last_result_full = result
            st.draft_result = result[:1200]
            logger.info("task_success chat_id=%s task_id=%s result_len=%s", chat_id, task_id, len(result))
            self.render_home(chat_id, notice="✅ Черновик готов. Можно протестировать prompt.")

        def on_error(err: str) -> None:
            st = self.sessions.get(chat_id)
            st.busy = False
            st.task_status = "failed"
            logger.error("task_failed chat_id=%s task_id=%s err=%s", chat_id, task_id, err)
            self.render_home(chat_id, notice=f"❌ Ошибка задачи: {err}")

        accepted = self.task_queue.submit(
            QueueTask(
                task_id=task_id,
                user_id=chat_id,
                timeout_sec=300,
                retries_left=1,
                fn=run_task,
                on_success=on_success,
                on_error=on_error,
            )
        )
        if not accepted:
            state.busy = False
            logger.info("task_duplicate_rejected chat_id=%s task_id=%s", chat_id, task_id)
            self.render_home(chat_id, notice="⏳ Такая же задача уже запущена")

    def _send_short_preview(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.draft_result:
            self.render_home(chat_id, notice="Черновик пока пуст")
            return
        self.bot.send_message(chat_id, f"📝 Черновик:\n\n```\n{state.draft_result}\n```", parse_mode="Markdown")
        logger.info("draft_sent chat_id=%s len=%s", chat_id, len(state.draft_result))

    def _send_full_result(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.last_result_full:
            self.render_home(chat_id, notice="Финальный результат пока пуст")
            return
        text = state.last_result_full
        if len(text) <= 3900:
            self.bot.send_message(chat_id, f"✅ Финал:\n\n```\n{text}\n```", parse_mode="Markdown")
            logger.info("final_sent_inline chat_id=%s len=%s", chat_id, len(text))
            return
        output = self.settings.local_data_dir / f"final_{chat_id}.txt"
        output.write_text(text, encoding="utf-8")
        with output.open("rb") as f:
            self.bot.send_document(chat_id, f, caption="✅ Финал")
        logger.info("final_sent_file chat_id=%s path=%s len=%s", chat_id, output, len(text))

    def _start_test_prompt(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.last_result_full:
            self.render_home(chat_id, notice="Сначала сгенерируй prompt")
            return
        state.test_chat_active = True
        state.test_history = []
        logger.info("prompt_test_started chat_id=%s", chat_id)
        self.render_home(chat_id, notice="🧪 Тест промпта включен. Пиши сообщения — бот ответит в test-режиме.")

    def _process_test_chat_message(self, chat_id: int, user_text: str) -> None:
        state = self.sessions.get(chat_id)
        if not state.test_chat_active:
            return
        system_prompt = state.last_result_full or ""
        history = "\n".join([f"USER: {u}\nBOT: {b}" for u, b in (state.test_history or [])[-8:]])
        prompt = (
            f"SYSTEM PROMPT:\n{system_prompt}\n\n"
            "Ниже тест-диалог. Ответь как AI Twin строго в заданном стиле.\n"
            f"{history}\nUSER: {user_text}\nBOT:"
        )
        try:
            reply = self.engine.run_prompt_job(prompt)
        except Exception as e:
            logger.exception("prompt_test_failed chat_id=%s", chat_id)
            self.bot.send_message(chat_id, f"❌ Ошибка теста: {e}")
            return
        (state.test_history or []).append((user_text, reply[:500]))
        self.bot.send_message(chat_id, reply[:3000])
        logger.info("prompt_test_reply chat_id=%s user_len=%s reply_len=%s", chat_id, len(user_text), len(reply))

    def _open_admin_panel(self, chat_id: int) -> None:
        models = ", ".join(self.engine.get_models())
        text = f"👑 Админ-панель\n\nМодели: {models}\nКлючей активных: {len(self.access.list_active_keys())}"
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("🔑 Показать все ключи", callback_data="admin_keys"))
        kb.add(InlineKeyboardButton("🤖 Показать модели", callback_data="admin_models"))
        kb.add(InlineKeyboardButton("♻️ Установить free-модели", callback_data="admin_set_free_models"))
        kb.add(InlineKeyboardButton("✍️ Задать модели вручную", callback_data="admin_set_models"))
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="refresh"))
        self._edit_or_send(chat_id, text, kb)

    def _show_all_keys(self, chat_id: int) -> None:
        rows = self.access.list_active_keys()
        if not rows:
            self.render_home(chat_id, notice="Активных ключей нет")
            return
        body = "\n".join([f"{uid}: `{k}`" for uid, k in rows])
        self.bot.send_message(chat_id, f"🔑 Активные ключи:\n{body}", parse_mode="Markdown")
        logger.info("admin_keys_sent chat_id=%s count=%s", chat_id, len(rows))

    def _show_models(self, chat_id: int) -> None:
        self.render_home(chat_id, notice=f"Текущие модели: {', '.join(self.engine.get_models())}")

    def _edit_or_send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup) -> None:
        state = self.sessions.get(chat_id)
        if state.status_message_id is None:
            sent = self.bot.send_message(chat_id, text, reply_markup=markup)
            state.status_message_id = sent.message_id
            return
        try:
            self.bot.edit_message_text(text, chat_id, state.status_message_id, reply_markup=markup)
        except Exception:
            logger.warning("edit_failed_resend chat_id=%s message_id=%s", chat_id, state.status_message_id)
            sent = self.bot.send_message(chat_id, text, reply_markup=markup)
            state.status_message_id = sent.message_id

    def render_home(self, chat_id: int, notice: str | None = None) -> None:
        state = self.sessions.get(chat_id)
        self._try_attach_web_upload(chat_id)

        has_access = self._has_access(chat_id)
        is_admin = chat_id == self.settings.admin_id
        target = state.selected_target or "—"
        export_loaded = state.loaded_export is not None

        checklist = (
            f"[1] Ключ: {'✅' if has_access else '❌'}\n"
            f"[2] Файл: {'✅' if export_loaded else '❌'}\n"
            f"[3] Цель: {'✅' if state.selected_target else '❌'}\n"
            f"[4] Статус: {state.task_status}"
        )

        text = (
            f"{t('ui.home_title', locale=state.lang)}\n\n"
            f"Пошаговый режим: сначала ключ, потом файл, потом цель, потом модуль.\n\n"
            f"{checklist}\n"
            f"Цель: {target}\n"
        )

        if state.ui_mode == "await_key":
            text += "\nВведите API-ключ сообщением ниже."
        elif state.ui_mode == "await_target":
            text += "\nВведите имя цели сообщением ниже."
        elif state.ui_mode == "await_models":
            text += "\nРежим админа: введите список моделей через запятую."

        if state.test_chat_active:
            text += "\n\n🧪 Тест промпта: ВКЛЮЧЕН"

        if notice:
            text += f"\n\n{notice}"

        markup = InlineKeyboardMarkup(row_width=1)

        # Шаг 1: нет ключа -> показываем только ключевой онбординг
        if not has_access and not is_admin:
            markup.add(InlineKeyboardButton("🔑 Ввести API ключ", callback_data="add_key"))
            markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh"))
            self._edit_or_send(chat_id, text, markup)
            return

        # Шаг 2: нет файла
        if not export_loaded:
            markup.add(InlineKeyboardButton("🌐 Загрузить файл через веб", callback_data="open_web"))
            markup.add(InlineKeyboardButton("📎 Или отправьте .json/.zip прямо в чат", callback_data="refresh"))
            if is_admin:
                markup.add(InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel"))
            markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh"))
            self._edit_or_send(chat_id, text, markup)
            return

        # Шаг 3: выбор цели
        if not state.selected_target:
            markup.add(InlineKeyboardButton("🎯 Выбрать цель", callback_data="pick_target"))
            markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh"))
            if is_admin:
                markup.add(InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel"))
            self._edit_or_send(chat_id, text, markup)
            return

        # Шаг 4: модули
        if state.last_result_full is None:
            for mod in self.registry.list_items():
                markup.add(InlineKeyboardButton(mod.title, callback_data=f"module:{mod.module_id}"))
            markup.add(InlineKeyboardButton("🎯 Сменить цель", callback_data="pick_target"))
            markup.add(InlineKeyboardButton("🔁 Повторить последний", callback_data="rerun_last"))
            if is_admin:
                markup.add(InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel"))
            markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh"))
            self._edit_or_send(chat_id, text, markup)
            return

        # После результата
        markup.add(InlineKeyboardButton("📝 Показать черновик", callback_data="show_draft"))
        markup.add(InlineKeyboardButton("✅ Показать финал", callback_data="approve_final"))
        if state.test_chat_active:
            markup.add(InlineKeyboardButton("🛑 Остановить тест промпта", callback_data="stop_test_prompt"))
        else:
            markup.add(InlineKeyboardButton("🧪 Запустить тест промпта", callback_data="start_test_prompt"))
        markup.add(InlineKeyboardButton("🔁 Сгенерировать заново", callback_data="rerun_last"))
        markup.add(InlineKeyboardButton("🎯 Сменить цель", callback_data="pick_target"))
        if is_admin:
            markup.add(InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel"))
        markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh"))

        self._edit_or_send(chat_id, text, markup)

    def run(self) -> None:
        logger.info("bot_run_loop_started")
        # Снимаем webhook, чтобы не конфликтовать с long polling.
        try:
            self.bot.remove_webhook()
            logger.info("webhook_removed_on_start")
        except Exception:
            logger.exception("webhook_remove_failed_on_start")

        while True:
            try:
                self.bot.infinity_polling(
                    timeout=60,
                    long_polling_timeout=60,
                    skip_pending=True,
                )
            except apihelper.ApiTelegramException as e:
                err = str(e)
                if "409" in err:
                    # Конфликт getUpdates: второй инстанс или активный webhook.
                    time.sleep(5)
                    try:
                        self.bot.remove_webhook()
                    except Exception:
                        logger.exception("webhook_remove_failed_after_409")
                    logger.warning("polling_conflict_409_retry")
                    continue
                logger.exception("polling_telegram_exception")
                raise
            except Exception:
                # transient network/telegram issues
                logger.exception("polling_transient_exception_retry")
                time.sleep(3)
                continue
