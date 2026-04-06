from flask import Flask, request, render_template_string, jsonify, send_file
import os
import requests
import zipfile
import json
import time
from config import BASE_URL, TG_BOT_TOKEN, LOCAL_DATA_DIR

app = Flask(__name__)

# СОВРЕМЕННЫЙ ШАБЛОН С ПРОГРЕСС-БАРОМ И DRAG & DROP
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Загрузка дампа | ИИ Биограф</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0f172a; --card-bg: rgba(30, 41, 59, 0.7); --border: rgba(255, 255, 255, 0.1); --accent: #3b82f6; --accent-hover: #2563eb; --text: #f8fafc; --text-muted: #94a3b8; --success: #10b981; --error: #ef4444; }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg); color: var(--text); display: flex; align-items: center; justify-content: center; min-height: 100vh; background-image: radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.15) 0px, transparent 50%), radial-gradient(at 100% 100%, rgba(139, 92, 246, 0.15) 0px, transparent 50%); }
        .container { width: 100%; max-width: 480px; padding: 2rem; background: var(--card-bg); border-radius: 1.5rem; border: 1px solid var(--border); box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); backdrop-filter: blur(10px); text-align: center; }
        h1 { font-size: 1.75rem; font-weight: 800; margin-bottom: 0.5rem; background: linear-gradient(to right, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        p.subtitle { color: var(--text-muted); margin-bottom: 2rem; font-size: 0.95rem; }
        .drop-zone { border: 2px dashed var(--border); border-radius: 1rem; padding: 3rem 2rem; cursor: pointer; transition: all 0.3s ease; background: rgba(15, 23, 42, 0.4); margin-bottom: 1.5rem; }
        .drop-zone:hover, .drop-zone.dragover { border-color: var(--accent); background: rgba(59, 130, 246, 0.05); }
        .icon { font-size: 3rem; margin-bottom: 1rem; color: var(--accent); }
        .text-sm { font-size: 0.875rem; margin-top: 0.5rem; }
        .text-muted { color: var(--text-muted); }
        .btn { background-color: var(--accent); color: white; border: none; padding: 0.875rem 2rem; border-radius: 0.75rem; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background-color 0.2s; width: 100%; }
        .btn:hover { background-color: var(--accent-hover); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .progress-container { display: none; margin-top: 1.5rem; text-align: left; }
        .progress-bar-bg { width: 100%; height: 8px; background-color: rgba(255,255,255,0.1); border-radius: 4px; overflow: hidden; margin-top: 0.5rem; }
        .progress-bar { width: 0%; height: 100%; background-color: var(--accent); transition: width 0.3s ease; }
        .status { margin-top: 0.5rem; font-size: 0.875rem; display: flex; justify-content: space-between; color: var(--text-muted); }
        .alert { margin-top: 1.5rem; padding: 1rem; border-radius: 0.75rem; display: none; font-size: 0.9rem; }
        .alert.success { background-color: rgba(16, 185, 129, 0.1); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.2); }
        .alert.error { background-color: rgba(239, 68, 68, 0.1); color: var(--error); border: 1px solid rgba(239, 68, 68, 0.2); }
    </style>
</head>
<body>
    <div class="container">
        <h1>Загрузка данных</h1>
        <p class="subtitle">Отправьте дамп истории Telegram для анализа</p>
        <div class="drop-zone" id="dropZone">
            <div class="icon">📄</div>
            <p>Нажмите или перетащите файл сюда</p>
            <p class="text-sm text-muted">Поддерживаются файлы <b>.json</b> или архивы <b>.zip</b></p>
            <input type="file" id="fileInput" accept=".json, .zip" style="display: none;">
        </div>
        <button class="btn" id="uploadBtn" disabled>Загрузить на сервер</button>
        <div class="progress-container" id="progressContainer">
            <div class="status">
                <span id="statusText">Подготовка...</span>
                <span id="percentText">0%</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar" id="progressBar"></div>
            </div>
        </div>
        <div class="alert" id="alertBox"></div>
    </div>
    <script>
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressBar = document.getElementById('progressBar');
        const percentText = document.getElementById('percentText');
        const statusText = document.getElementById('statusText');
        const alertBox = document.getElementById('alertBox');
        let selectedFile = null;
        const urlParams = new URLSearchParams(window.location.search);
        const userId = urlParams.get('user_id');

        if (!userId) {
            showAlert('Ошибка: Отсутствует user_id. Откройте ссылку из бота.', 'error');
            dropZone.style.pointerEvents = 'none';
        }
        dropZone.addEventListener('click', () => { if (userId) fileInput.click(); });
        fileInput.addEventListener('change', (e) => { handleFile(e.target.files[0]); });
        dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => { dropZone.classList.remove('dragover'); });
        dropZone.addEventListener('drop', (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); if (e.dataTransfer.files.length) { handleFile(e.dataTransfer.files[0]); } });

        function handleFile(file) {
            if (!file) return;
            if (!file.name.endsWith('.json') && !file.name.endsWith('.zip')) {
                showAlert("Пожалуйста, загрузите файл .json или архив .zip", "error");
                return;
            }
            selectedFile = file;
            dropZone.innerHTML = `<div class="icon">✅</div><p><b>${file.name}</b></p><p class="text-sm text-muted">${(file.size / (1024*1024)).toFixed(2)} MB</p>`;
            uploadBtn.disabled = false;
            hideAlert();
        }

        uploadBtn.addEventListener('click', () => {
            if (!selectedFile || !userId) return;
            const formData = new FormData();
            formData.append('file', selectedFile);
            formData.append('user_id', userId);
            uploadBtn.style.display = 'none';
            dropZone.style.display = 'none';
            progressContainer.style.display = 'block';

            const xhr = new XMLHttpRequest();
            // Используем BASE_URL из конфига для запроса
            const uploadUrl = "{{ base_url }}/bots/upload";
            xhr.open('POST', uploadUrl, true);
            
            xhr.upload.onprogress = function(e) {
                if (e.lengthComputable) {
                    const percentComplete = Math.round((e.loaded / e.total) * 100);
                    progressBar.style.width = percentComplete + '%';
                    percentText.innerText = percentComplete + '%';
                    statusText.innerText = percentComplete === 100 ? 'Распаковка и обработка...' : 'Отправка...';
                }
            };
            xhr.onload = function() {
                if (xhr.status === 200) {
                    statusText.innerText = 'Готово!';
                    progressBar.style.backgroundColor = 'var(--success)';
                    showAlert('Файл успешно загружен! Возвращайтесь в Telegram-бота.', 'success');
                } else {
                    let err = "Произошла ошибка при загрузке";
                    try { err = JSON.parse(xhr.responseText).error || err; } catch(e) {}
                    showAlert(err, 'error');
                    uploadBtn.style.display = 'block';
                    dropZone.style.display = 'block';
                    progressContainer.style.display = 'none';
                }
            };
            xhr.send(formData);
        });
        function showAlert(msg, type) { alertBox.innerText = msg; alertBox.className = 'alert ' + type; alertBox.style.display = 'block'; }
        function hideAlert() { alertBox.style.display = 'none'; }
    </script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
@app.route('/upload', methods=['GET'])
@app.route('/bots/upload', methods=['GET'])
def render_form():
    return render_template_string(HTML_TEMPLATE, base_url=BASE_URL)

@app.route('/', methods=['POST'])
@app.route('/upload', methods=['POST'])
@app.route('/bots/upload', methods=['POST'])
def upload_file():
    try:
        user_id = request.form.get('user_id')
        if not user_id: return jsonify({"error": "user_id не указан"}), 400
        if 'file' not in request.files: return jsonify({"error": "Нет файла в запросе"}), 400
        
        file = request.files['file']
        if file.filename == '': return jsonify({"error": "Файл не выбран"}), 400
        
        if file and (file.filename.endswith('.json') or file.filename.endswith('.zip')):
            os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
            filepath = os.path.join(LOCAL_DATA_DIR, f"upload_{user_id}.json")
            
            if file.filename.endswith('.zip'):
                try:
                    with zipfile.ZipFile(file, 'r') as zip_ref:
                        result_filename = None
                        for name in zip_ref.namelist():
                            if name.endswith('.json') and not name.split('/')[-1].startswith('.'):
                                result_filename = name
                                break
                        if not result_filename:
                            return jsonify({"error": "Ни одного .json файла не найдено!"}), 400
                        
                        with zip_ref.open(result_filename) as zf, open(filepath, 'wb') as f:
                            f.write(zf.read())
                except zipfile.BadZipFile:
                    return jsonify({"error": "Ошибка ZIP-архива"}), 400
            else:
                file.save(filepath)
            
            if TG_BOT_TOKEN:
                size_mb = os.path.getsize(filepath) / (1024*1024)
                msg = f"✅ Файл загружен! Размер: {size_mb:.1f} МБ.\n\nНажмите кнопку ниже для старта."
                url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
                payload = {
                    "chat_id": user_id, 
                    "text": msg,
                    "reply_markup": {"inline_keyboard": [[{"text": "▶️ Начать обработку!", "callback_data": "process_web_upload"}]]}
                }
                requests.post(url, json=payload)
            return jsonify({"status": "success"}), 200
        return jsonify({"error": "Неверный формат"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/download/<file_id>', methods=['GET'])
@app.route('/bots/download/<file_id>', methods=['GET'])
def download_file_route(file_id):
    file_id = "".join(c for c in file_id if c.isalnum() or c == '-')
    info_path = os.path.join(LOCAL_DATA_DIR, "downloads", f"{file_id}.json")
    if not os.path.exists(info_path): return "❌ Ссылка недействительна.", 404
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        if time.time() > info["expires_at"]:
            os.remove(info_path)
            return "⏳ Ссылка истекла.", 403
        return send_file(info["filepath"], as_attachment=True)
    except Exception as e: return f"Ошибка: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)