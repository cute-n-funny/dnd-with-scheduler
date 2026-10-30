FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATABASE_PATH=/data/scheduler.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py storage.py logic.py ./
CMD ["python", "bot.py"]
