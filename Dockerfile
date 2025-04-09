# 使用多架构兼容的基础镜像
FROM --platform=$BUILDPLATFORM python:3.9-slim AS builder

# 安装跨架构兼容的依赖
RUN apt-get update && \
    apt-get install -y \
    ffmpeg=7:4.3.6-0+deb11u1 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# 添加架构识别参数
ARG TARGETARCH
RUN echo "Target Architecture: $TARGETARCH" > /arch-info.txt

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /hls && \
    chmod 777 /hls && \
    mkdir -p /app/config

CMD ["python", "app.py"]

EXPOSE 50086
