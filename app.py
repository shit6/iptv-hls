import json
import os
import subprocess
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, request, abort, send_from_directory, Response

app = Flask(__name__)

# 配置参数
CONFIG_PATH = '/app/config/streams.json'
HLS_ROOT = '/hls'
TIMEOUT = 30  # 流超时时间（秒）
CHECK_INTERVAL = 5  # 配置检查间隔（秒）
STREAM_START_TIMEOUT = 10  # 推流启动超时时间（秒）

# 全局变量
config_lock = threading.Lock()
streams_lock = threading.Lock()
last_modified = 0
TOKEN = None
config_data = {}
STREAMS = {}
active_streams = {}

class StreamProcess:
    def __init__(self, stream_id, url):
        self.stream_id = stream_id
        self.url = url
        self.process = None
        self.last_access = datetime.now()
        self.output_dir = Path(HLS_ROOT) / stream_id
        self.running = False
        self.ready_event = threading.Event()  # 推流就绪事件

    def start(self):
        try:
            # 清理旧目录并创建新目录
            shutil.rmtree(self.output_dir, ignore_errors=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            
            # FFmpeg推流命令
            cmd = [
                'ffmpeg',
                '-i', self.url,
                '-c', 'copy',
                '-f', 'hls',
                '-hls_time', '2',        # 更短的分片时间
                '-hls_list_size', '5',   # 更小的列表长度
                '-hls_flags', 'delete_segments+append_list+temp_file',
                '-start_number', '0',    # 明确起始编号
                '-hls_base_url', f'/{self.stream_id}/',
                str(self.output_dir / 'index.m3u8')
            ]
            
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self.running = True
            app.logger.info(f"Started stream: {self.stream_id}")
            
            # 启动监控线程
            threading.Thread(target=self._monitor_ready, daemon=True).start()
        except Exception as e:
            app.logger.error(f"Failed to start stream {self.stream_id}: {str(e)}")
            self.running = False

    def _monitor_ready(self):
        """监控HLS文件生成的线程"""
        start_time = time.time()
        while time.time() - start_time < STREAM_START_TIMEOUT:
            if (self.output_dir / 'index.m3u8').exists():
                self.ready_event.set()
                app.logger.info(f"Stream {self.stream_id} ready in {time.time()-start_time:.1f}s")
                return
            time.sleep(0.1)
        
        app.logger.error(f"Stream {self.stream_id} failed to start in {STREAM_START_TIMEOUT}s")
        self.stop()

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.output_dir, ignore_errors=True)
        self.running = False
        self.ready_event.clear()
        app.logger.info(f"Stopped stream: {self.stream_id}")

def load_config():
    global last_modified, TOKEN, STREAMS, config_data
    try:
        current_modified = os.path.getmtime(CONFIG_PATH)
        if current_modified == last_modified:
            return

        with config_lock:
            if current_modified == last_modified:
                return

            with open(CONFIG_PATH) as f:
                config_data = json.load(f)
                new_token = config_data['token']
                new_streams = {s['stream_id']: s['url'] for s in config_data['list']}

            TOKEN = new_token
            STREAMS = new_streams.copy()
            last_modified = current_modified
            app.logger.info(f"Config reloaded at {datetime.fromtimestamp(last_modified)}")
    except Exception as e:
        app.logger.error(f"Error loading config: {str(e)}")

def config_watcher():
    """配置监视线程"""
    while True:
        load_config()
        time.sleep(CHECK_INTERVAL)

def cleanup_worker():
    """流清理线程"""
    while True:
        time.sleep(10)
        now = datetime.now()
        with streams_lock:
            to_remove = []
            for stream_id, sp in active_streams.items():
                if (now - sp.last_access).total_seconds() > TIMEOUT:
                    sp.stop()
                    to_remove.append(stream_id)
            for sid in to_remove:
                del active_streams[sid]

@app.before_request
def handle_request():
    """请求预处理"""
    path = request.path.strip('/').split('/')
    if len(path) > 0 and path[0] in STREAMS:
        stream_id = path[0]
        with streams_lock:
            if stream_id in active_streams:
                active_streams[stream_id].last_access = datetime.now()

@app.route('/<stream_id>/index.m3u8')
def serve_hls(stream_id):
    # Token验证
    if request.args.get('token') != TOKEN:
        abort(502, description="Invalid token")

    # 检查流是否存在
    if stream_id not in STREAMS:
        abort(404, description="Stream not found")

    with streams_lock:
        sp = active_streams.get(stream_id)
        
        # 需要启动新推流
        if not sp or not sp.running:
            sp = StreamProcess(stream_id, STREAMS[stream_id])
            sp.start()
            if sp.running:
                active_streams[stream_id] = sp
            else:
                abort(503, description="Failed to start stream")

        # 等待流就绪
        if not sp.ready_event.wait(timeout=5):
            abort(503, description="Stream initialization timeout")

    # 读取并修改m3u8内容
    m3u8_path = sp.output_dir / 'index.m3u8'
    with open(m3u8_path, 'r') as f:
        content = f.read()
    modified_content = content.replace('.ts', f'.ts?token={TOKEN}')
    
    return Response(modified_content, mimetype='application/vnd.apple.mpegurl')

@app.route('/<stream_id>/<filename>')
def serve_ts(stream_id, filename):
    # 验证token
    if request.args.get('token') != TOKEN:
        abort(502, description="Invalid token")
    
    # 检查流是否存在
    if stream_id not in STREAMS:
        abort(404, description="Stream not found")
    
    # 处理带参数的请求
    base_filename = filename.split('?')[0]
    stream_dir = Path(HLS_ROOT) / stream_id
    file_path = stream_dir / base_filename
    
    if not file_path.exists():
        abort(404, description="Segment not found")
    
    return send_from_directory(str(stream_dir), base_filename)

@app.route('/txt')
def channel_list():
    """频道列表接口"""
    if request.args.get('token') != TOKEN:
        abort(502, description="Invalid token")
    
    base_url = request.host_url.rstrip('/')
    output = []
    
    with config_lock:
        for channel in config_data['list']:
            stream_url = f"{base_url}/{channel['stream_id']}/index.m3u8?token={TOKEN}"
            output.append(f"{channel['name']},{stream_url}")
    
    return Response('\n'.join(output), mimetype='text/plain')

if __name__ == '__main__':
    load_config()
    threading.Thread(target=config_watcher, daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=50086, threaded=True)
