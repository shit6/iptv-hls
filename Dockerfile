# 第一阶段：构建FFmpeg（Alpine优化版）
FROM alpine:3.18 as ffmpeg_builder

# 配置镜像源（国内镜像加速）
RUN echo "https://mirrors.aliyun.com/alpine/v3.18/main" > /etc/apk/repositories && \
    echo "https://mirrors.aliyun.com/alpine/v3.18/community" >> /etc/apk/repositories

# 安装编译工具及依赖
RUN apk add --no-cache \
    build-base curl nasm yasm coreutils autoconf automake git pkgconf \
    x264-dev x265-dev libvpx-dev fdk-aac-dev lame-dev opus-dev \
    openssl-dev libwebp-dev zlib-dev

# 手动编译安装旧版 lame (3.100-r0)
RUN curl -LO http://downloads.sourceforge.net/lame/lame-3.100.tar.gz && \
    tar xzf lame-3.100.tar.gz && \
    cd lame-3.100 && \
    ./configure --prefix=/usr/local --enable-shared --libdir=/usr/local/lib && \
    make -j$(nproc) && \
    make install && \
    rm -rf ../lame-3.100*

# 编译FFmpeg（关键：保留动态库符号）
RUN git clone --depth 1 https://git.ffmpeg.org/ffmpeg.git && \
    cd ffmpeg && \
    ./configure \
    --prefix=/usr/local \
    --enable-gpl --enable-nonfree \
    --enable-libx264 --enable-libx265 --enable-libvpx \
    --enable-libfdk-aac --enable-libmp3lame --enable-libopus \
    --enable-openssl --enable-libwebp \
    --enable-shared --disable-static \
    --extra-cflags="-I/usr/local/include" \
    --extra-ldflags="-L/usr/local/lib -Wl,-rpath=/usr/local/lib" && \
    make -j$(nproc) && \
    make install

# 清理构建缓存（保留动态库）
RUN rm -rf /ffmpeg

# 第二阶段：构建Python应用
FROM python:3.9-alpine3.18

# 配置镜像源（与构建阶段一致）
RUN echo "https://mirrors.aliyun.com/alpine/v3.18/main" > /etc/apk/repositories && \
    echo "https://mirrors.aliyun.com/alpine/v3.18/community" >> /etc/apk/repositories

# 安装运行时依赖
RUN apk add --no-cache \
    libstdc++ libgcc tzdata python3 \
    libvpx fdk-aac opus x264-libs x265-libs libwebp \
    libffi openssl

# 从构建阶段复制FFmpeg及其所有动态库
COPY --from=ffmpeg_builder /usr/local/bin/ffmpeg /usr/local/bin/
COPY --from=ffmpeg_builder /usr/local/lib/ /usr/local/lib/

# 设置动态库路径（关键：覆盖系统路径）
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# 安装Python依赖（锁定版本）
RUN pip install --no-cache-dir \
    "flask==2.0.3" \
    "werkzeug==2.0.3" \
    cython

WORKDIR /app

# 复制整个项目到容器
COPY . .

# 移动静态资源到根目录（根据你的需求调整）
RUN mv src/static . && \
    mv src/templates . 

# 编译Python模块
RUN apk add --no-cache build-base python3-dev libffi-dev openssl-dev && \
    # 动态获取路径
    PY_INC=$(python3 -c "import sysconfig; print(sysconfig.get_path('include'))") && \
    PY_LIB=$(python3 -c "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))") && \
    # 编译hls模块
    cd src && \
    cython --embed -3 -o hls.c hls.py && \
    gcc -Os -fPIC -shared \
    -I${PY_INC} \
    -L${PY_LIB} -Wl,-rpath=${PY_LIB} \
    hls.c -o hls.cpython-39-x86_64-linux-gnu.so \
    -lpython3.9 -lffi -lssl -lcrypto && \
    # 清理工作
    rm hls.py hls.c && \
    cd .. && \
    apk del build-base python3-dev libffi-dev openssl-dev

# 设置时区
ENV TZ=Asia/Shanghai

# 暴露端口
EXPOSE 50086

# 启动命令
CMD ["python3", "main.py"]
