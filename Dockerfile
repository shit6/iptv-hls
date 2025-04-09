# 使用官方多架构镜像
FROM --platform=$BUILDPLATFORM python:3.9-slim

# 安装依赖（兼容多架构）
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /hls && \
    chmod 777 /hls && \
    mkdir -p /app/config

CMD ["python", "app.py"]

EXPOSE 50086
