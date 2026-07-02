FROM python:3.12-slim

WORKDIR /app

# app.py 用的是 Python 内置的 urllib + zipfile 下载/解压 xray，
# 不依赖 curl / unzip 命令行工具，这里只保留 HTTPS 请求所需的 CA 证书。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY app.py ./

CMD ["python", "app.py"]
