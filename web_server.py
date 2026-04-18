from __future__ import annotations

import requests
from flask import Flask, jsonify, render_template_string, request

from core.config import load_settings
from modules.secure_store import SecureBlobStore

settings = load_settings()
secure_store = SecureBlobStore(settings.local_data_dir / "secure_uploads")
app = Flask(__name__)

HTML = """
<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Twin Upload</title></head>
<body style="font-family:Arial;max-width:640px;margin:40px auto;">
<h2>Загрузка Telegram-экспорта</h2>
<p>Поддержка .json и .zip. Файл на диске хранится только в зашифрованном виде.</p>
<form action="{{ base_url }}/bots/upload" method="post" enctype="multipart/form-data">
  <input type="hidden" name="user_id" value="{{ user_id }}" />
  <input type="file" name="file" accept=".json,.zip" required />
  <button type="submit">Загрузить</button>
</form>
</body>
</html>
"""


@app.get("/")
@app.get("/bots/upload")
def upload_form():
    user_id = request.args.get("user_id", "")
    return render_template_string(HTML, base_url=settings.base_url, user_id=user_id)


@app.post("/bots/upload")
def upload_post():
    user_id = request.form.get("user_id", "").strip()
    file = request.files.get("file")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if not file:
        return jsonify({"error": "file is required"}), 400
    filename = (file.filename or "").lower()
    if not (filename.endswith(".json") or filename.endswith(".zip")):
        return jsonify({"error": "Only .json/.zip are allowed"}), 400

    blob_id = f"upload_{user_id}"
    secure_store.save_encrypted(blob_id, file.read())

    try:
        requests.post(
            f"https://api.telegram.org/bot{settings.tg_bot_token}/sendMessage",
            json={
                "chat_id": int(user_id),
                "text": "✅ Файл загружен. Данные зашифрованы. Нажмите Обновить в боте.",
            },
            timeout=20,
        )
    except Exception:
        pass

    return jsonify({"status": "ok", "blob_id": blob_id})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
