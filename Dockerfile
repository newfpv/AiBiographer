FROM python:3.10-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем ВСЕ файлы кода
COPY *.py .

# Создаем папку для локальных файлов и сырых биографий
RUN mkdir -p /app/data

# Запускаем скрипт
CMD ["python", "-u", "tg_bot.py"]