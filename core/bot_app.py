from __future__ import annotations

import threading
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import Settings
from core.engine import AIEngine, DEFAULT_FREE_MODELS, GeminiPool
from core.state import SessionStore
from modules.access_store import AccessStore
from modules.builtin_modules import run_biography_module, run_prompt_module
from modules.i18n import t
from modules.module_registry import BotModule, ModuleRegistry
from modules.secure_store import SecureBlobStore
from modules.task_queue import QueueTask, TaskQueue
from modules.telegram_data import all_messages, detect_candidates, load_export_bytes


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

    def _bootstrap_saved_keys_into_pool(self) -> None:
        for _, key in self.access.list_active_keys():
            self.engine.pool.add_key(key)

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
            state = self.sessions.get(message.chat.id)
            state.lang = self.settings.default_lang
            state.ui_mode = "await_key" if not self._has_access(message.chat.id) else "home"
            self.render_home(
                message.chat.id,
                notice="/start: 1) введите ключ 2) загрузите чат 3) выберите цель 4) выберите модуль",
            )

        @self.bot.message_handler(content_types=["document"])
        def handle_doc(message):
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

    def _try_attach_web_upload(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        blob_id = f"upload_{chat_id}"
        try:
            raw = self.secure_store.read_decrypted(blob_id)
            if raw:
                state.task_status = "upload_parsing"
                state.loaded_export = load_export_bytes(raw, filename_hint="upload.json")
                self.secure_store.delete_blob(blob_id)
                state.task_status = "idle"
        except Exception:
            pass

    def _has_access(self, chat_id: int) -> bool:
        if chat_id == self.settings.admin_id:
            return True
        return self.access.get_active_key(chat_id) is not None

    def _handle_key_input(self, chat_id: int, key: str) -> None:
        state = self.sessions.get(chat_id)
        if self.engine.validate_key(key):
            self.access.upsert_key(chat_id, key, is_active=True)
            self.engine.pool.add_key(key)
            state.submitted_api_key = key
            state.ui_mode = "home"
            self.render_home(chat_id, notice=t("ui.key_saved", locale=state.lang))
        else:
            self.render_home(chat_id, notice=t("ui.key_bad", locale=state.lang))

    def _show_target_picker(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.loaded_export:
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

    def _select_top_target(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        names = detect_candidates(all_messages(state.loaded_export or {}), limit=1)
        if names:
            state.selected_target = names[0]
            self.render_home(chat_id, notice=f"Цель выбрана: {names[0]}")
        else:
            self.render_home(chat_id, notice="Не удалось авто-выбрать цель")

    def _start_module(self, chat_id: int, module_id: str) -> None:
        state = self.sessions.get(chat_id)
        module = self.registry.get(module_id)
        if not module:
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

        def run_task() -> str:
            state.task_status = "running"
            return module.run(self.engine, state.loaded_export, state.selected_target)

        def on_success(result: str) -> None:
            st = self.sessions.get(chat_id)
            st.busy = False
            st.task_status = "done"
            st.last_result_full = result
            st.draft_result = result[:1200]
            self.render_home(chat_id, notice="✅ Черновик готов. Можно протестировать prompt.")

        def on_error(err: str) -> None:
            st = self.sessions.get(chat_id)
            st.busy = False
            st.task_status = "failed"
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
            self.render_home(chat_id, notice="⏳ Такая же задача уже запущена")

    def _send_short_preview(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.draft_result:
            self.render_home(chat_id, notice="Черновик пока пуст")
            return
        self.bot.send_message(chat_id, f"📝 Черновик:\n\n```\n{state.draft_result}\n```", parse_mode="Markdown")

    def _send_full_result(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.last_result_full:
            self.render_home(chat_id, notice="Финальный результат пока пуст")
            return
        text = state.last_result_full
        if len(text) <= 3900:
            self.bot.send_message(chat_id, f"✅ Финал:\n\n```\n{text}\n```", parse_mode="Markdown")
            return
        output = self.settings.local_data_dir / f"final_{chat_id}.txt"
        output.write_text(text, encoding="utf-8")
        with output.open("rb") as f:
            self.bot.send_document(chat_id, f, caption="✅ Финал")

    def _start_test_prompt(self, chat_id: int) -> None:
        state = self.sessions.get(chat_id)
        if not state.last_result_full:
            self.render_home(chat_id, notice="Сначала сгенерируй prompt")
            return
        state.test_chat_active = True
        state.test_history = []
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
            self.bot.send_message(chat_id, f"❌ Ошибка теста: {e}")
            return
        (state.test_history or []).append((user_text, reply[:500]))
        self.bot.send_message(chat_id, reply[:3000])

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
            sent = self.bot.send_message(chat_id, text, reply_markup=markup)
            state.status_message_id = sent.message_id

    def render_home(self, chat_id: int, notice: str | None = None) -> None:
        state = self.sessions.get(chat_id)
        self._try_attach_web_upload(chat_id)
        has_access = self._has_access(chat_id)
        status = t("ui.status_ready", locale=state.lang) if has_access else t("ui.status_no_access", locale=state.lang)
        target = state.selected_target or "—"
        export_flag = "✅" if state.loaded_export else "❌"

        checklist = (
            f"[1] Ключ: {'✅' if has_access else '❌'} | "
            f"[2] Файл: {export_flag} | "
            f"[3] Цель: {'✅' if state.selected_target else '❌'} | "
            f"[4] Статус: {state.task_status}"
        )

        mode_text = ""
        if state.ui_mode == "await_key":
            mode_text = f"\n{t('ui.mode_key', locale=state.lang)}"
        elif state.ui_mode == "await_target":
            mode_text = f"\n{t('ui.mode_target', locale=state.lang)}"
        elif state.ui_mode == "await_models":
            mode_text = "\nРежим: ввод списка моделей"

        text = (
            f"{t('ui.home_title', locale=state.lang)}\n\n"
            f"{t('ui.home_desc', locale=state.lang)}\n\n"
            f"{status}\n"
            f"{checklist}\n"
            f"Цель: {target}{mode_text}\n"
        )
        if state.test_chat_active:
            text += "\n🧪 Test prompt: ВКЛЮЧЕН\n"
        if notice:
            text += f"\n{notice}\n"

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton(t("ui.btn_web", locale=state.lang), callback_data="open_web"),
            InlineKeyboardButton(t("ui.btn_target", locale=state.lang), callback_data="pick_target"),
        )

        for mod in self.registry.list_items():
            markup.add(InlineKeyboardButton(mod.title, callback_data=f"module:{mod.module_id}"))
            markup.add(InlineKeyboardButton("ℹ️", callback_data=f"module_info:{mod.module_id}"))

        markup.add(
            InlineKeyboardButton("🔁 Повторить последний", callback_data="rerun_last"),
            InlineKeyboardButton("📝 Черновик", callback_data="show_draft"),
        )
        markup.add(
            InlineKeyboardButton("✅ Утвердить финал", callback_data="approve_final"),
            InlineKeyboardButton("🧪 Тест промпта", callback_data="start_test_prompt"),
        )
        markup.add(InlineKeyboardButton("🛑 Остановить тест", callback_data="stop_test_prompt"))

        markup.add(
            InlineKeyboardButton(t("ui.btn_add_key", locale=state.lang), callback_data="add_key"),
            InlineKeyboardButton(t("ui.btn_revoke", locale=state.lang), callback_data="revoke_key"),
        )
        if chat_id == self.settings.admin_id:
            markup.add(InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel"))
        markup.add(
            InlineKeyboardButton(t("ui.btn_lang", locale=state.lang), callback_data="lang_ru"),
            InlineKeyboardButton(t("ui.btn_refresh", locale=state.lang), callback_data="refresh"),
        )

        self._edit_or_send(chat_id, text, markup)

    def run(self) -> None:
        self.bot.infinity_polling(timeout=60, long_polling_timeout=60)
