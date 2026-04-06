import os
import json
import time
import uuid
from dotenv import load_dotenv

# Загружаем .env если он есть (полезно для локального запуска)
load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
API_KEYS_STR = os.getenv("API_KEYS", "")
API_KEYS = [k.strip() for k in API_KEYS_STR.split(",") if k.strip()]

# Конвертируем ADMIN_ID в int, чтобы сравнения в коде работали корректно
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

LOCAL_DATA_DIR = os.getenv("LOCAL_DATA_DIR", "/app/data")
WHITELIST_FILE = os.path.join(LOCAL_DATA_DIR, "whitelist.json")

# Базовый URL для ссылок
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip('/')

if not TG_BOT_TOKEN or not API_KEYS:
    print("❌ ОШИБКА: Не заданы TG_BOT_TOKEN или API_KEYS в .env!")
    exit(1)

class UserSession:
    def __init__(self):
        self.data = None
        self.unique_names = []
        self.name_map = {}
        self.current_name_idx = 0
        self.days_data = {}
        self.total_msgs = 0
        self.limit = 0
        self.chunks = []
        self.mode = ""
        self.target_user = ""
        self.status_msg_id = None
        self.is_fetching = False
        self.stop_requested = False
        self.current_status_text = ""
        self.active_threads = {} 
        self.processed_count = 0
        self.total_blocks = 0
        self.top_contacts = []
        self.owner_name = ""

def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {int(x): "Неизвестный" for x in data}
                return {int(k): v for k, v in data.items()}
        except: pass
    return {ADMIN_ID: "Admin"}

def save_whitelist(wl):
    os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False)

def is_allowed(user_id, whitelist_dict):
    return user_id == ADMIN_ID or user_id in whitelist_dict

def format_eta(seconds):
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0: return f"{hours}ч {mins}м {secs}с"
    return f"{mins}м {secs}с"

def cleanup_files(*filepaths):
    for path in filepaths:
        try:
            if os.path.exists(path): os.remove(path)
        except: pass

def create_download_link(filepath):
    file_id = str(uuid.uuid4())
    downloads_dir = os.path.join(LOCAL_DATA_DIR, "downloads")
    os.makedirs(downloads_dir, exist_ok=True)
    
    expires_at = time.time() + 600 
    info = {"filepath": filepath, "expires_at": expires_at}
    
    with open(os.path.join(downloads_dir, f"{file_id}.json"), "w", encoding="utf-8") as f:
        json.dump(info, f)
        
    return f"{BASE_URL}/bots/download/{file_id}"