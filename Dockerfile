# syntax=docker/dockerfile:1

FROM --platform=$TARGETPLATFORM python:3.9-slim-bookworm

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 配置APT源
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list && \
    sed -i 's/security.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list

# 安装依赖
RUN apt-get update && \
    apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 创建工作目录
WORKDIR /app

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制应用文件
COPY app.py .

# 创建用户和权限设置
RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /hls /app/config && \
    chown appuser:appuser /hls /app/config

USER appuser

# 容器配置
EXPOSE 50086
CMD ["python", "app.py"]
