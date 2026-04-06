import telebot
from telebot import apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import json
import os
import glob
import threading

from config import TG_BOT_TOKEN, LOCAL_DATA_DIR, ADMIN_ID, BASE_URL, UserSession, load_whitelist, save_whitelist, is_allowed
from data_parser import parse_messages, chunk_data, get_all_messages
from pipelines import run_generation, run_recompression_task, trigger_finish, send_to_admin

# Настройки таймаутов для работы с тяжелыми файлами
apihelper.SESSION_TIME_TO_LIVE = 5 * 60
apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300

bot = telebot.TeleBot(TG_BOT_TOKEN)
sessions = {}
whitelist = load_whitelist()

# Функция-помощник для безопасного удаления сообщений
def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def cb_admin_panel(call):
    if call.message.chat.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("➕ Добавить пользователя", callback_data="admin_add"),
        InlineKeyboardButton("🗑 Удалить пользователя", callback_data="admin_del"),
        InlineKeyboardButton("📜 Посмотреть вайтлист", callback_data="admin_wl"),
        InlineKeyboardButton("🔙 Закрыть", callback_data="admin_close")
    )
    bot.edit_message_text("⚙️ **Админ-панель**", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "admin_add")
def cb_admin_add(call):
    if call.message.chat.id != ADMIN_ID: return
    msg = bot.send_message(call.message.chat.id, "ID пользователя:")
    bot.register_next_step_handler(msg, process_admin_add)

def process_admin_add(message):
    if message.chat.id != ADMIN_ID: return
    if message.text.isdigit():
        uid = int(message.text)
        whitelist[uid] = "Добавлен вручную"
        save_whitelist(whitelist)
        bot.send_message(message.chat.id, f"✅ Добавлен: `{uid}`", parse_mode="Markdown")
    else: bot.send_message(message.chat.id, "❌ Нужен ID цифрами")

@bot.callback_query_handler(func=lambda call: call.data == "admin_del")
def cb_admin_del(call):
    if call.message.chat.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup(row_width=1)
    for uid, name in whitelist.items():
        if uid != ADMIN_ID:
            markup.add(InlineKeyboardButton(f"❌ Удалить: {name} ({uid})", callback_data=f"del_uid_{uid}"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text("Кого удалить?", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("del_uid_"))
def cb_del_uid(call):
    if call.message.chat.id != ADMIN_ID: return
    uid = int(call.data.split("_")[2])
    if uid in whitelist:
        del whitelist[uid]
        save_whitelist(whitelist)
        bot.answer_callback_query(call.id, "Удален!", show_alert=True)
    cb_admin_del(call) 

@bot.callback_query_handler(func=lambda call: call.data == "admin_wl")
def cb_admin_wl(call):
    if call.message.chat.id != ADMIN_ID: return
    wl_text = f"📜 **Вайтлист ({len(whitelist)}):**\n" + "".join(f"• `{uid}` — {name}\n" for uid, name in whitelist.items())
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(wl_text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "admin_close")
def cb_admin_close(call):
    if call.message.chat.id != ADMIN_ID: return
    safe_delete_message(call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    uname = message.from_user.username or message.from_user.first_name or "Без_имени"
    if chat_id in whitelist and whitelist[chat_id] != uname:
        whitelist[chat_id] = uname
        save_whitelist(whitelist)
    
    if not is_allowed(chat_id, whitelist):
        bot.send_message(chat_id, f"⛔️ Отказано. ID `{chat_id}` не в списке.", parse_mode="Markdown")
        return

    markup = InlineKeyboardMarkup(row_width=1)
    upload_url = f"{BASE_URL}/bots/upload?user_id={chat_id}"
    markup.add(InlineKeyboardButton("🌐 Загрузить файл (>20 МБ)", url=upload_url))
    markup.add(InlineKeyboardButton("▶️ Я загрузил файл!", callback_data="process_web_upload"))

    if chat_id == ADMIN_ID:
        markup.add(InlineKeyboardButton("📂 Взять локальный result.json", callback_data="use_local_file"))
        markup.add(InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin_panel"))
    
    bot.reply_to(message, f"👋 Привет! Твой ID: `{chat_id}`", parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "use_local_file")
def cb_use_local_file(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_ID: return 
    local_path = os.path.join(LOCAL_DATA_DIR, "result.json")
    if not os.path.exists(local_path):
        bot.answer_callback_query(call.id, "Файл не найден!", show_alert=True)
        return
    safe_delete_message(chat_id, call.message.message_id)
    try:
        with open(local_path, 'r', encoding='utf-8') as f: data = json.load(f)
        init_session(chat_id, data)
        ask_rename(chat_id)
    except Exception as e: bot.send_message(chat_id, f"❌ Ошибка: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "process_web_upload")
def cb_process_web_upload(call):
    chat_id = call.message.chat.id
    filepath = os.path.join(LOCAL_DATA_DIR, f"upload_{chat_id}.json")
    if not os.path.exists(filepath):
        bot.answer_callback_query(call.id, "❌ Сначала загрузи на сайте.", show_alert=True)
        return
    safe_delete_message(chat_id, call.message.message_id)
    try:
        with open(filepath, 'r', encoding='utf-8') as f: data = json.load(f)
        init_session(chat_id, data)
        ask_rename(chat_id)
    except Exception as e: bot.send_message(chat_id, f"❌ Ошибка: {e}")

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    chat_id = message.chat.id
    if not is_allowed(chat_id, whitelist): return
    if not (message.document.file_name.endswith('.json') or message.document.file_name.endswith('.zip')):
        bot.send_message(chat_id, "❌ Нужен .json или .zip")
        return
    if message.document.file_size > 20 * 1024 * 1024:
        bot.send_message(chat_id, "❌ Файл > 20 МБ. Используй сайт.")
        return

    msg = bot.send_message(chat_id, "⏳ Скачиваю...")
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        data = json.loads(downloaded)
        init_session(chat_id, data)
        safe_delete_message(chat_id, msg.message_id)
        ask_rename(chat_id)
    except Exception as e: bot.send_message(chat_id, f"❌ Ошибка: {e}")

def init_session(chat_id, data):
    msg_counts = {}
    all_msgs = get_all_messages(data)
    for m in all_msgs:
        if m.get('type') == 'message' and m.get('from'): 
            name = m.get('from')
            msg_counts[name] = msg_counts.get(name, 0) + 1
    session = UserSession()
    session.data = data
    sorted_names = sorted(msg_counts.items(), key=lambda x: x[1], reverse=True)
    session.unique_names = [k for k, v in sorted_names]
    session.top_contacts = sorted_names[:10] 
    owner_info = data.get('personal_information', {})
    owner_name = f"{owner_info.get('first_name', '')} {owner_info.get('last_name', '')}".strip()
    session.owner_name = owner_name or (session.unique_names[0] if session.unique_names else "Владелец")
    sessions[chat_id] = session

def ask_rename(chat_id):
    session = sessions[chat_id]
    if len(session.unique_names) > 20 and session.current_name_idx == 0:
        for name in session.unique_names: session.name_map[name] = name
        session.current_name_idx = len(session.unique_names)
        prepare_limit(chat_id)
        return
    if session.current_name_idx < len(session.unique_names):
        curr = session.unique_names[session.current_name_idx]
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(f"Оставить '{curr[:20]}'", callback_data="rename_skip"))
        if len(session.unique_names) > 1:
            markup.add(InlineKeyboardButton("⏭ Пропустить всех", callback_data="rename_skip_all"))
        msg = bot.send_message(chat_id, f"👥 Нашел: *{curr}*\nНовое имя или пропуск:", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler(msg, process_rename_input)
    else: prepare_limit(chat_id)

def process_rename_input(message):
    chat_id = message.chat.id
    session = sessions.get(chat_id)
    if not session or not message.text: return
    session.name_map[session.unique_names[session.current_name_idx]] = message.text.strip()
    session.current_name_idx += 1
    ask_rename(chat_id)

@bot.callback_query_handler(func=lambda call: call.data == "rename_skip")
def cb_rename_skip(call):
    chat_id = call.message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    curr = session.unique_names[session.current_name_idx]
    session.name_map[curr] = curr
    session.current_name_idx += 1
    safe_delete_message(chat_id, call.message.message_id)
    ask_rename(chat_id)

@bot.callback_query_handler(func=lambda call: call.data == "rename_skip_all")
def cb_rename_skip_all(call):
    chat_id = call.message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    bot.clear_step_handler_by_chat_id(chat_id)
    while session.current_name_idx < len(session.unique_names):
        curr = session.unique_names[session.current_name_idx]
        session.name_map[curr] = curr
        session.current_name_idx += 1
    safe_delete_message(chat_id, call.message.message_id)
    prepare_limit(chat_id)

def prepare_limit(chat_id):
    session = sessions[chat_id]
    msg = bot.send_message(chat_id, "⏳ Анализ сообщений...")
    session.days_data = parse_messages(session.data, session.name_map, lambda c, t: None)
    safe_delete_message(chat_id, msg.message_id)
    session.total_msgs = sum(len(v) for v in session.days_data.values())
    dl = 2000 if session.total_msgs < 20000 else 5000
    session.limit = dl 
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton(f"✅ Оставить ({dl})", callback_data=f"limit_default_{dl}"))
    msg_l = bot.send_message(chat_id, f"📊 Всего: {session.total_msgs}\n🤖 Лимит: {dl}", reply_markup=markup)
    bot.register_next_step_handler(msg_l, process_custom_limit)

def process_custom_limit(message):
    chat_id = message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    if message.text.isdigit() and int(message.text) > 0:
        session.limit = int(message.text)
        ask_mode(chat_id)
    else:
        msg = bot.send_message(chat_id, "Введите число:")
        bot.register_next_step_handler(msg, process_custom_limit)

@bot.callback_query_handler(func=lambda call: call.data.startswith("limit_default_"))
def cb_limit_default(call):
    chat_id = call.message.chat.id
    bot.clear_step_handler_by_chat_id(chat_id)
    session = sessions.get(chat_id)
    if not session: return
    session.limit = int(call.data.split("_")[2])
    safe_delete_message(chat_id, call.message.message_id)
    ask_mode(chat_id)

def ask_mode(chat_id):
    session = sessions[chat_id]
    session.chunks = chunk_data(session.days_data, session.limit)
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("🚀 Полный конвейер", callback_data="mode_full"))
    if glob.glob(os.path.join(LOCAL_DATA_DIR, "raw_biography_*.txt")):
         markup.add(InlineKeyboardButton("🔄 Продолжить", callback_data="mode_resume"))
    bot.send_message(chat_id, f"✂️ Блоков: {len(session.chunks)}", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode_"))
def cb_mode(call):
    chat_id = call.message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    session.mode = call.data.split("_")[1]
    safe_delete_message(chat_id, call.message.message_id)
    if session.mode == "resume":
        files = glob.glob(os.path.join(LOCAL_DATA_DIR, "raw_biography_*.txt"))
        markup = InlineKeyboardMarkup(row_width=1)
        for f in files:
            user = os.path.basename(f).replace("raw_biography_", "").replace(".txt", "")
            markup.add(InlineKeyboardButton(user[:40], callback_data=f"target_{user[:20]}"))
        bot.send_message(chat_id, "Для кого продолжаем?", reply_markup=markup)
    else:
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(InlineKeyboardButton(f"👑 {session.owner_name[:20]}", callback_data=f"target_{session.owner_name[:20]}"))
        btns, seen = [], set([session.owner_name])
        for orig in session.unique_names:
            new = session.name_map.get(orig, orig)
            if new not in seen:
                seen.add(new)
                btns.append(InlineKeyboardButton(new[:40], callback_data=f"target_{new[:20]}"))
            if len(btns) >= 30: break
        markup.add(*btns)
        markup.add(InlineKeyboardButton("✍️ Ввести вручную", callback_data="manual_target"))
        bot.send_message(chat_id, "Чью биографию делаем?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "manual_target")
def cb_manual_target(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions: return
    msg = bot.send_message(chat_id, "Напиши имя:")
    bot.register_next_step_handler(msg, process_manual_target)

def process_manual_target(message):
    chat_id = message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    session.target_user = message.text.strip()
    session.stop_requested = False
    start_generation_flow(chat_id, session, message.from_user.username)

def start_generation_flow(chat_id, session, username):
    bot.send_message(chat_id, f"🎯 Цель: *{session.target_user}*", parse_mode="Markdown")
    sm = bot.send_message(chat_id, "⏳ Сбор данных...")
    raw_path = os.path.join(LOCAL_DATA_DIR, f"parsed_chat_{session.target_user}.txt")
    with open(raw_path, 'w', encoding='utf-8') as f:
        for idx, c in enumerate(session.chunks): 
            f.write(f"=== БЛОК {idx}: {c['title']} ===\n" + "\n".join(c['lines']) + "\n\n")
    send_to_admin(bot, chat_id, username, session.target_user, raw_path, "СЫРАЯ ПЕРЕПИСКА")
    session.status_msg_id = sm.message_id
    threading.Thread(target=run_generation, args=(bot, chat_id, session, username)).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("target_"))
def cb_target(call):
    chat_id = call.message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    session.target_user = call.data.split("_", 1)[1]
    session.stop_requested = False 
    safe_delete_message(chat_id, call.message.message_id)
    start_generation_flow(chat_id, session, call.from_user.username)

@bot.callback_query_handler(func=lambda call: call.data.startswith("stop_"))
def cb_stop_process(call):
    chat_id = call.message.chat.id
    session = sessions.get(chat_id)
    if not session: return
    session.stop_requested = True
    session.is_fetching = False
    bot.answer_callback_query(call.id, "Остановка...")

@bot.callback_query_handler(func=lambda call: call.data.startswith("recompress_"))
def cb_recompress(call):
    chat_id = call.message.chat.id
    target = call.data.split("_", 1)[1]
    session = sessions.get(chat_id)
    if session: session.stop_requested = False
    bot.edit_message_text(f"⏳ Сжатие {target}...", chat_id, call.message.message_id)
    threading.Thread(target=run_recompression_task, args=(bot, chat_id, target, call.from_user.username, session)).start()

@bot.callback_query_handler(func=lambda call: call.data.startswith("finish_"))
def cb_finish(call):
    target = call.data.split("_", 1)[1]
    bot.edit_message_text(f"✅ Завершено для {target}", call.message.chat.id, call.message.message_id)
    trigger_finish(bot, call.message.chat.id, target)

print("🤖 Бот запущен.")
bot.infinity_polling(timeout=60, long_polling_timeout=60)