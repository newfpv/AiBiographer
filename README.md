# AI Biographer / AI Twin Prompt Builder

Переписанный бот с архитектурой **толстое ядро (`core/`) + тонкие расширяемые модули (`modules/`)**.

## Текущий UX
1. `/start`
2. Пользователь вводит свой API-ключ
3. Загружает Telegram-экспорт
4. Выбирает цель
5. Нажимает нужный модуль

Интерфейс — одно меню (одно редактируемое сообщение).

## Приватность данных
- Файл, загруженный через веб, сохраняется только в **зашифрованном виде** (`Fernet`) и удаляется после чтения ботом.
- При загрузке файла напрямую в Telegram — парсинг идет из байтов, без хранения raw-файла на диске.

## Модульная архитектура
Добавление новой функции делается как новый модуль, без переписывания ядра:
- зарегистрировать `BotModule` в `ModuleRegistry`
- реализовать `run(...)` в новом модуле
- кнопка появится в общем меню модулей

Встроенные модули сейчас:
- `🧬 AI Twin Prompt`
- `📖 Биография`

## Структура
- `main.py` — единая точка входа (`bot`/`web`) + базовая настройка логирования
- `core/config.py` — env + settings
- `core/engine.py` — key-pool + fallback моделей + валидация ключа
- `core/bot_app.py` — Telegram-оркестрация, in-memory state и меню модулей
- `core/web_app.py` — Flask web uploader
- `modules/module_registry.py` — реестр модулей
- `modules/builtin_modules.py` — встроенные модули
- `modules/telegram_data.py` — разбор Telegram экспорта + статистика стиля
- `modules/prompt_pipeline.py` — шаблон генератора финального промпта
- `modules/secure_store.py` — шифрованные blob-и загрузок
- `modules/access_store.py` — SQLite ключи пользователей
- `modules/i18n.py` + `locales/ru.json` — i18n слой

## Запуск
```bash
pip install -r requirements.txt
python main.py bot
```

Веб-загрузка:
```bash
python main.py web
```

## `.env`
```env
TG_BOT_TOKEN=...
ADMIN_ID=123456789
API_KEYS=key1,key2
BASE_URL=http://localhost:5000
LOCAL_DATA_DIR=/app/data
DEFAULT_LANG=ru
SECURE_BLOB_KEY=some_secret_for_fernet_derivation
```
