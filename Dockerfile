# syntax=docker/dockerfile:1
# LiAuto-Mate 单容器（方案 §9：单容器/单卷/单端口；Playwright 内置供重登）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LIAM_DATA_DIR=/data \
    LIAM_LOGIN_HEADLESS=1

# 系统依赖：libatomic1 是 playwright 内嵌 node 的硬前置（--with-deps 装不了它
# 自己启动要的库——鸡生蛋，实测 11-20）+ tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatomic1 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY liam ./liam
# Python 依赖（login extra：playwright+numpy+pillow）
RUN pip install --no-cache-dir '.[login]' \
    && python -m playwright install --with-deps chromium

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

CMD ["python", "-m", "liam.cli", "serve"]
