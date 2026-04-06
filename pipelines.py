import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import LOCAL_DATA_DIR, ADMIN_ID, cleanup_files, format_eta, API_KEYS, create_download_link
from ai_manager import call_gemini_with_retries

def send_to_admin(bot, chat_id, username, target, filepath, stage=""):
    try:
        uname = f"@{username}" if username else f"ID:{chat_id}"
        cap = f"🕵️‍♂️ **ОТЧЕТ ({stage}):** {uname}\n🎯 **Цель:** {target}"
        
        if filepath and os.path.exists(filepath):
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if size_mb < 15: # Порог 15 МБ для стабильности
                bot.send_document(ADMIN_ID, open(filepath, 'rb'), caption=cap, parse_mode="Markdown", timeout=300)
            else:
                link = create_download_link(filepath)
                bot.send_message(ADMIN_ID, f"{cap}\n\n⚠️ Файл огромный ({size_mb:.1f} МБ).\n🔗 [Скачать файл]({link})\n_Ссылка активна 10 минут._", parse_mode="Markdown", disable_web_page_preview=True)
        else:
            bot.send_message(ADMIN_ID, cap, parse_mode="Markdown")
            
    except Exception as e: print(f"❌ Ошибка админ-отчета: {e}")

def trigger_finish(bot, chat_id, target):
    prompt_stage_2 = (f"Исходя из данных файла, напиши красивое, связное и подробное досье на человека по имени {target}. "
                      f"Разбей текст на логические блоки: 'Характер и личность', 'Отношения', 'Работа и увлечения', 'Хронология и события'. "
                      f"Обязательно сохраняй даты для важных и средних событий. Игнорируй скучный бытовой пересказ.")
    bot.send_message(chat_id, f"💡 **Финальный промпт для итоговой обработки:**\n\n`{prompt_stage_2}`", parse_mode="Markdown")
    
    raw_path = os.path.join(LOCAL_DATA_DIR, f"parsed_chat_{target}.txt")
    output_filename = os.path.join(LOCAL_DATA_DIR, f"raw_biography_{target}.txt")
    final_filename = os.path.join(LOCAL_DATA_DIR, f"FINAL_BIO_{target}.txt")
    web_upload_path = os.path.join(LOCAL_DATA_DIR, f"upload_{chat_id}.json")
    cleanup_files(output_filename, final_filename, raw_path, web_upload_path)

def run_generation(bot, chat_id, session, username):
    process_start_time = time.time()
    max_workers = len(API_KEYS)
    
    session.is_fetching = True
    session.active_threads.clear()
    session.processed_count = 0
    session.current_status_text = "🧬 **ЭТАП 1: Анализ переписки**"

    print(f"\n🚀 СТАРТ ГЕНЕРАЦИИ | Цель: {session.target_user} | Потоков: {max_workers}\n")

    def status_updater():
        while session.is_fetching:
            if session.stop_requested: break
            elapsed = int(time.time() - process_start_time)
            
            eta_text = "вычисляется..."
            if session.processed_count > 0:
                avg_time = elapsed / session.processed_count
                left = session.total_blocks - session.processed_count
                eta_text = format_eta(avg_time * left)

            threads_info = "\n".join([f"Блок {k}: {v}" for k, v in list(session.active_threads.items())[:10]])
            if not threads_info: threads_info = "Ожидание..."

            text = (
                f"{session.current_status_text}\n"
                f"📊 Прогресс: {session.processed_count} из {session.total_blocks}\n\n"
                f"⏱ Прошло: {format_eta(elapsed)}\n"
                f"⏳ Осталось: ~{eta_text}\n\n"
                f"📡 **Потоки в работе:**\n{threads_info}"
            )
            try:
                markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🛑 Стоп", callback_data=f"stop_{session.target_user}"))
                bot.edit_message_text(text, chat_id, session.status_msg_id, reply_markup=markup)
            except: pass
            time.sleep(5)

    output_filename = os.path.join(LOCAL_DATA_DIR, f"raw_biography_{session.target_user}.txt")
    completed_blocks = set()
    if session.mode == "resume" and os.path.exists(output_filename):
        with open(output_filename, 'r', encoding='utf-8') as f:
            completed_blocks = set(re.findall(r"--- БЛОК \d+: (.*?) ---", f.read()))
    else:
        with open(output_filename, 'w', encoding='utf-8') as f:
            f.write(f"=== СЫРЫЕ ДАННЫЕ: {session.target_user} ===\n\n")

    prompt1 = f"""Ты — эксперт-профайлер. Проанализируй этот фрагмент переписки и извлеки ДОЛГОСРОЧНЫЕ ФАКТЫ о человеке «{session.target_user}».
КАТЕГОРИЧЕСКИ ЗАПРЕЩАЕТСЯ:
1. Пересказывать диалог (запрещено писать "он сказал", "он спросил").
2. Писать про мелкий бытовой мусор (кто что ел, сломанные роутеры, мокрые кофты).

ВМЕСТО ЭТОГО заполни эту анкету (если данных для пункта нет - просто не пиши его):
- [Профессия и работа]: ...
- [Хобби и увлечения]: ...
- [Отношения и окружение]: (с кем общается, кто семья)
- [Имущество и питомцы]: (авто, животные, гаджеты)
- [Черты характера и привычки]: ...
- [События с датами]: (Выписывай события СРЕДНЕЙ и ВЫСОКОЙ значимости с указанием дат. Например: "Ноябрь 2022 - начал ходить в зал", "Запланировал свадьбу на 17 число", "Купил машину").

Текст переписки:
"""

    blocks_to_process = [c for c in session.chunks if c['title'] not in completed_blocks]
    session.total_blocks = len(blocks_to_process)
    
    threading.Thread(target=status_updater, daemon=True).start()

    def process_chunk_task(idx, chunk_data):
        if session.stop_requested: return None
        time.sleep(idx * 0.5) 
        out_text, _ = call_gemini_with_retries(prompt1, '\n'.join(chunk_data['lines']), session, "БЛОК", idx)
        return idx, chunk_data['title'], out_text

    write_index = 0
    results_buffer = {}
    status_lock = threading.Lock()
    
    if session.total_blocks > 0:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_chunk_task, i, chunk): i for i, chunk in enumerate(blocks_to_process)}
            for future in as_completed(futures):
                if session.stop_requested: break
                res = future.result()
                if res:
                    res_idx, title, out_text = res
                    with status_lock:
                        results_buffer[res_idx] = (title, out_text)
                        while write_index in results_buffer:
                            t, txt = results_buffer.pop(write_index)
                            with open(output_filename, 'a', encoding='utf-8') as f:
                                f.write(f"--- БЛОК {write_index}: {t} ---\n{txt}\n\n")
                            write_index += 1
                        session.processed_count += 1

    if session.stop_requested: return
    
    size_mb_output = os.path.getsize(output_filename) / (1024 * 1024)
    if size_mb_output < 15:
        bot.send_document(chat_id, open(output_filename, 'rb'), caption="🎉 Этап 1 завершен.", timeout=300)
    else:
        link = create_download_link(output_filename)
        bot.send_message(chat_id, f"🎉 Этап 1 завершен!\n*(Размер: {size_mb_output:.1f} МБ)*\n\n🔗 [Скачать файл]({link})\n_Ссылка активна 10 минут._", parse_mode="Markdown", disable_web_page_preview=True)
        
    send_to_admin(bot, chat_id, username, session.target_user, output_filename, stage="ЭТАП 1 (СЫРЫЕ ФАКТЫ)")
    
    blocks, current_block = [], []
    with open(output_filename, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('--- БЛОК'):
                if current_block: blocks.append('\n'.join(current_block))
                current_block = [line.strip()]
            elif not line.startswith('===') and (line.strip() or current_block):
                current_block.append(line.strip())
        if current_block: blocks.append('\n'.join(current_block))

    print(f"📦 Собрано блоков для ЭТАПА 2: {len(blocks)}") 

    if len(blocks) <= 5:
        print("⚡ Блоков мало, этап 2 пропускается.")
        session.is_fetching = False
        trigger_finish(bot, chat_id, session.target_user)
        return

    final_filename = os.path.join(LOCAL_DATA_DIR, f"FINAL_BIO_{session.target_user}.txt")
    with open(final_filename, 'w', encoding='utf-8') as f:
        f.write(f"=== ИТОГОВАЯ БИОГРАФИЯ: {session.target_user} ===\n\n")

    batches = [blocks[i:i + 5] for i in range(0, len(blocks), 5)]
    
    prompt2 = f"""Ты — главный аналитик-биограф. Ниже собраны профильные анкеты пользователя «{session.target_user}», собранные из разных частей его переписки.
Твоя задача — объединить их в один плотный, информативный текст.
ПРАВИЛА:
1. Безжалостно УДАЛЯЙ ДУБЛИКАТЫ фактов.
2. СОХРАНЯЙ ДАТЫ для значимых событий (встречи, покупки, поездки, изменения в жизни).
3. Оставь только фундаментальную информацию о человеке (его личность, работа, связи, имущество, хронология важных событий).
Сформируй итоговый ответ строго по категориям.
Тексты для анализа:\n"""

    session.active_threads.clear()
    session.processed_count = 0
    session.total_blocks = len(batches)
    session.current_status_text = "🔄 **ЭТАП 2: Сжатие фактов**"
    process_start_time = time.time()

    def process_batch_task(idx, batch_data):
        if session.stop_requested: return None
        time.sleep(idx * 0.5)
        batch_text = '\n\n'.join(batch_data)
        out_text, _ = call_gemini_with_retries(prompt2, batch_text, session, "СЖАТИЕ", idx)
        return idx, out_text

    write_index = 0
    results_buffer.clear()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_batch_task, i, batch): i for i, batch in enumerate(batches)}
        for future in as_completed(futures):
            if session.stop_requested: break
            res = future.result()
            if res:
                res_idx, out_text = res
                with status_lock:
                    results_buffer[res_idx] = out_text
                    while write_index in results_buffer:
                        txt = results_buffer.pop(write_index)
                        with open(final_filename, 'a', encoding='utf-8') as f:
                            f.write(f"--- ЧАСТЬ {write_index+1} ---\n{txt}\n\n")
                        write_index += 1
                    session.processed_count += 1

    if session.stop_requested: return
    session.is_fetching = False
    try: bot.edit_message_text("✅ Готово!", chat_id, session.status_msg_id)
    except: pass
    
    size_mb_final = os.path.getsize(final_filename) / (1024 * 1024)
    if size_mb_final < 15:
        bot.send_document(chat_id, open(final_filename, 'rb'), caption="🏆 Итоговая биография.", timeout=300)
    else:
        link = create_download_link(final_filename)
        bot.send_message(chat_id, f"🏆 Итоговая биография готова!\n*(Размер: {size_mb_final:.1f} МБ)*\n\n🔗 [Скачать файл]({link})\n_Ссылка активна 10 минут._", parse_mode="Markdown", disable_web_page_preview=True)

    send_to_admin(bot, chat_id, username, session.target_user, final_filename, stage="ЭТАП 2 (ФИНАЛ)")
    
    if len(batches) > 5:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🗜 Сжать еще раз", callback_data=f"recompress_{session.target_user}"))
        markup.add(InlineKeyboardButton("✅ Оставить так", callback_data=f"finish_{session.target_user}"))
        bot.send_message(chat_id, f"⚠️ Файлов много: {len(batches)}. Сжать еще?", reply_markup=markup)
    else:
        trigger_finish(bot, chat_id, session.target_user)

def run_recompression_task(bot, chat_id, target, username, session):
    final_filename = os.path.join(LOCAL_DATA_DIR, f"FINAL_BIO_{target}.txt")
    blocks, current_block = [], []
    with open(final_filename, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('--- ЧАСТЬ'):
                if current_block: blocks.append('\n'.join(current_block))
                current_block = [line.strip()]
            elif not line.startswith('===') and (line.strip() or current_block):
                current_block.append(line.strip())
        if current_block: blocks.append('\n'.join(current_block))

    batches = [blocks[i:i + 5] for i in range(0, len(blocks), 5)]
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🛑 Стоп", callback_data=f"stop_{target}"))
    msg = bot.send_message(chat_id, f"🔄 Повторное сжатие. Будет файлов: {len(batches)}", reply_markup=markup)
    
    temp_filename = final_filename + ".tmp"
    with open(temp_filename, 'w', encoding='utf-8') as f:
        f.write(f"=== ИТОГОВАЯ БИОГРАФИЯ (СЖАТАЯ): {target} ===\n\n")

    prompt2 = f"""Ты — главный редактор-биограф. Очисти текст от повторяющихся фактов и объедини его в мощное досье на «{target}».
Сохраняй даты только для важных или средних по значимости событий. Бытовой мусор удаляй. Тексты:\n"""
    
    def process_batch_task(idx, batch_data):
        if session and session.stop_requested: return None
        time.sleep(idx * 0.5)
        batch_text = '\n\n'.join(batch_data)
        out_text, _ = call_gemini_with_retries(prompt2, batch_text, session, "РЕКУРСИЯ", idx)
        return idx, out_text

    max_workers = len(API_KEYS)
    session.active_threads.clear()
    processed_count, write_index = 0, 0
    results_buffer = {}
    status_lock = threading.Lock()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_batch_task, i, batch): i for i, batch in enumerate(batches)}
        for future in as_completed(futures):
            if session and session.stop_requested: break
            res = future.result()
            if res:
                res_idx, out_text = res
                with status_lock:
                    results_buffer[res_idx] = out_text
                    while write_index in results_buffer:
                        txt = results_buffer.pop(write_index)
                        with open(temp_filename, 'a', encoding='utf-8') as f:
                            f.write(f"--- ЧАСТЬ {write_index+1} ---\n{txt}\n\n")
                        write_index += 1
                    processed_count += 1
                    
                    threads_info = "\n".join([f"Блок {k}: {v}" for k, v in list(session.active_threads.items())[:10]])
                    text_out = f"🔄 **Повторное сжатие**\n🧩 Собрано: {processed_count} из {len(batches)}\n\n📡 **Потоки в работе:**\n{threads_info}"
                    try: bot.edit_message_text(text_out, chat_id, msg.message_id, reply_markup=markup)
                    except: pass

    if session and session.stop_requested: return
    os.replace(temp_filename, final_filename)
    
    size_mb_final = os.path.getsize(final_filename) / (1024 * 1024)
    if size_mb_final < 15:
        bot.send_document(chat_id, open(final_filename, 'rb'), caption="🏆 СЖАТИЕ ЗАВЕРШЕНО!", timeout=300)
    else:
        link = create_download_link(final_filename)
        bot.send_message(chat_id, f"🏆 СЖАТИЕ ЗАВЕРШЕНО!\n*(Размер: {size_mb_final:.1f} МБ)*\n\n🔗 [Скачать файл]({link})\n_Ссылка активна 10 минут._", parse_mode="Markdown", disable_web_page_preview=True)
        
    send_to_admin(bot, chat_id, username, target, final_filename, stage="ЭТАП 3 (РЕКУРСИЯ)")

    if len(batches) > 5:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🗜 Сжать еще раз", callback_data=f"recompress_{target}"))
        markup.add(InlineKeyboardButton("✅ Оставить так", callback_data=f"finish_{target}"))
        bot.send_message(chat_id, f"⚠️ Файлов еще много: {len(batches)}. Сжать?", reply_markup=markup)
    else:
        trigger_finish(bot, chat_id, target)