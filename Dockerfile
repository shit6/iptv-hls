FROM --platform=$TARGETPLATFORM python:3.9-slim-bookworm

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 配置APT源
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list && \
    sed -i 's/security.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list

# 安装依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg=7:4.3.6-0+deb11u1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py .

RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /hls /app/config && \
    chown appuser:appuser /hls /app/config

USER appuser

EXPOSE 50086
CMD ["python", "app.py"]
