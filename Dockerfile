FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static

EXPOSE 8115

# 单 worker：115 影库是 I/O 型应用，单进程足够且最省内存
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8115", "--workers", "1"]
