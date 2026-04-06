import time
import re
import math
import threading
from google import genai
from google.genai import types
from config import API_KEYS

# Точные системные имена моделей из официальной документации Google
MODEL_FALLBACK_LIST = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite-preview", 
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

SAFE_CONFIG = types.GenerateContentConfig(
    safety_settings=[
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]
)

# Храним состояние моделей для каждого ключа индивидуально
api_key_states = {k: {"unban_time": 0, "in_use": False, "exhausted_models": set()} for k in API_KEYS}
key_lock = threading.Lock()

def acquire_key():
    while True:
        with key_lock:
            now = time.time()
            for k, state in api_key_states.items():
                if not state["in_use"] and now >= state["unban_time"] and len(state["exhausted_models"]) < len(MODEL_FALLBACK_LIST):
                    state["in_use"] = True
                    return k
        time.sleep(1)

def release_key(key, cooldown_seconds=0):
    with key_lock:
        api_key_states[key]["in_use"] = False
        api_key_states[key]["unban_time"] = time.time() + cooldown_seconds

def call_gemini_with_retries(prompt, text, session, step_name, idx):
    start_t = time.time()
    while True:
        # Проверка остановки
        if session and session.stop_requested: 
            if idx in session.active_threads: del session.active_threads[idx]
            return None, 0
        
        # ПОКАЗЫВАЕМ В ИНТЕРФЕЙСЕ, ЧТО ПОТОК В ОЧЕРЕДИ
        if session: session.active_threads[idx] = "⏳ Ждет свободный ключ..."
        
        api_key = acquire_key()
        key_mask = api_key[:8] + "..."
        
        available_models = [m for m in MODEL_FALLBACK_LIST if m not in api_key_states[api_key]["exhausted_models"]]
        
        if not available_models:
            print(f"🚨 Ключ {key_mask} исчерпал лимиты ВООБЩЕ ВСЕХ МОДЕЛЕЙ! Бан на 24 часа.")
            release_key(api_key, 86400) 
            continue

        current_model = available_models[0]
        print(f"▶️ [{step_name} {idx}] {current_model} | Ключ: {key_mask}")
        
        # КЛЮЧ ПОЛУЧЕН — ПОТОК В РАБОТЕ
        if session: session.active_threads[idx] = f"🚀 Отправка ({current_model})"
        
        client = genai.Client(api_key=api_key)
        try:
            response = client.models.generate_content(
                model=current_model, 
                contents=prompt + text,
                config=SAFE_CONFIG
            )
            out_text = response.text.strip() if response.text else "⚠️ [БЛОК ПРОПУЩЕН: Пустой ответ]"
            
            # УСПЕХ: Ключ отдыхает 15 секунд
            release_key(api_key, 15)
            elapsed = int(time.time() - start_t)
            
            # Убираем поток из списка интерфейса, так как он успешно завершил свой блок
            if session and idx in session.active_threads: 
                del session.active_threads[idx]
                
            print(f"✅ [{step_name} {idx}] Готов за {elapsed}с (Ключ: {key_mask})")
            return out_text, elapsed
            
        except Exception as e:
            err_msg = str(e).lower()
            
            # 1. Лимиты (429)
            if '429' in err_msg or 'resource_exhausted' in err_msg:
                if "daily" in err_msg or "quota exceeded" in err_msg:
                    print(f"💀 Ключ {key_mask}: Дневной лимит на {current_model}. Меняем модель...")
                    if session: session.active_threads[idx] = f"🔄 Смена модели | {current_model}"
                    with key_lock:
                        api_key_states[api_key]["exhausted_models"].add(current_model)
                    release_key(api_key, 1) 
                else: 
                    match = re.search(r'retry in (\d+(?:\.\d+)?)s', err_msg)
                    wait_time = math.ceil(float(match.group(1))) + 5 if match else 25
                    if session: session.active_threads[idx] = f"⏳ Сон ключа {wait_time}с"
                    print(f"⏳ RPM Лимит ({key_mask}). Ждем {wait_time}с...")
                    release_key(api_key, wait_time)
                    time.sleep(1)
                
            # 2. Модель не найдена (404)
            elif '404' in err_msg or 'not_found' in err_msg:
                print(f"⚠️ Ошибка 404: Модель {current_model} недоступна. Вычеркиваем...")
                if session: session.active_threads[idx] = f"🚫 Отключена | {current_model}"
                with key_lock:
                    api_key_states[api_key]["exhausted_models"].add(current_model)
                release_key(api_key, 1) 
                
            # 3. Сервер Гугла прилег (503/500)
            elif '503' in err_msg or '500' in err_msg:
                print(f"❌ Ошибка 503 ({key_mask}). Гугл перегружен. Спим 10с...")
                if session: session.active_threads[idx] = f"❌ Гугл 503 (Сон 10с)"
                release_key(api_key, 10)
                time.sleep(10)
                
            # 4. Прочее (включая Safety)
            else:
                if 'safety' in err_msg or 'finish_reason' in err_msg:
                    release_key(api_key, 2)
                    if session and idx in session.active_threads: del session.active_threads[idx]
                    return "⚠️ [БЛОК ПРОПУЩЕН: Жесткая ошибка безопасности]", int(time.time() - start_t)
                
                print(f"❓ Ошибка ({key_mask}): {err_msg[:80]}")
                if session: session.active_threads[idx] = "❓ Ошибка"
                release_key(api_key, 10)
                time.sleep(5)