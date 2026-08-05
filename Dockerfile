FROM docker.m.daocloud.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 构建期网络（QNAP 实测：容器直连不到 Debian/PyPI；走 NAS 上的 mihomo 代理。
# 换网络环境时用 --build-arg BUILD_PROXY=... 覆盖）
ARG BUILD_PROXY=http://192.168.1.107:7890

RUN apt-get -o Acquire::http::Proxy="$BUILD_PROXY" -o Acquire::https::Proxy="$BUILD_PROXY" update \
    && apt-get -o Acquire::http::Proxy="$BUILD_PROXY" -o Acquire::https::Proxy="$BUILD_PROXY" \
       install -y --no-install-recommends tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt --proxy "$BUILD_PROXY"

COPY app ./app
COPY static ./static

EXPOSE 8115

# 单 worker：115 影库是 I/O 型应用，单进程足够且最省内存
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8115", "--workers", "1"]
