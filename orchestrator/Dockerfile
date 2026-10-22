FROM python:3.11-slim

WORKDIR /app

# git — для операций с репозиториями (clone/push/PR); curl — служебный.
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# Контейнер работает от root, а монтируемый workspace принадлежит пользователю
# хоста. Без safe.directory git падает с "dubious ownership".
RUN git config --global --add safe.directory '*'

# Корень изолированных workspace (runs/). При bind-mount перекрывается хостовой
# директорией; фактическую гарантию 0777 даёт ensure_runs_dir_writable при старте.
RUN mkdir -p /app/workspace/runs && chmod 0777 /app/workspace/runs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Запускаем одновременно FastAPI gateway и Temporal Worker.
CMD ["sh", "-c", "umask 000; python worker.py & uvicorn main:app --host 0.0.0.0 --port 8000"]
