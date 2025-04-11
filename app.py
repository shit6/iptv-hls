import json
import os
import subprocess
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from flask import Flask, request, abort, send_from_directory, Response, jsonify
from flask import render_template_string
import html

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
            
            # 从全局配置获取hls参数
            with config_lock:  # 确保线程安全
                hls_time = config_data.get('hls_time', '3')  # 默认值3
                hls_list_size = config_data.get('hls_list_size', '12')  # 默认值12

            # FFmpeg推流命令
            cmd = [
                'ffmpeg',
                '-i', self.url,
                '-c', 'copy',
                '-f', 'hls',
                '-hls_time', hls_time,        # 更短的分片时间
                '-hls_list_size', hls_list_size,  # 使用配置的hls_list_size
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
    """带分组标识的频道列表接口"""
    if request.args.get('token') != TOKEN:
        abort(403, description="Invalid token")
    
    base_url = request.host_url.rstrip('/')
    output = []
    
    with config_lock:
        # 使用有序字典维护分组顺序
        groups = {}
        current_group = None
        
        for channel in config_data['list']:
            group_name = channel.get('group', '未分组')
            
            # 遇到新分组时添加分组标识
            if group_name != current_group:
                output.append(f"{group_name},#genre#")
                current_group = group_name
                groups.setdefault(group_name, [])
            
            # 生成频道条目
            stream_url = f"{base_url}/{channel['stream_id']}/index.m3u8?token={TOKEN}"
            groups[group_name].append(f"{channel['name']},{stream_url}")
        
        # 合并最终输出（保持分组出现顺序）
        final_output = []
        seen_groups = set()
        for line in output:
            if line.endswith(',#genre#'):
                group = line.split(',#genre#')[0]
                if group not in seen_groups:
                    seen_groups.add(group)
                    final_output.append(line)
                    final_output.extend(groups[group])
            else:
                final_output.append(line)
    
    return Response('\n'.join(final_output), mimetype='text/plain')

@app.route('/m3u')
def generate_m3u():
    if request.args.get('token') != TOKEN:
        abort(403, description="Invalid token")

    base_url = request.host_url.rstrip('/')
    output = ["#EXTM3U"]
    
    with config_lock:
        for channel in config_data['list']:
            # 处理特殊字符
            safe_name = quote(channel['name'].strip().replace(' ','_'), safe='')
            stream_url = f"{base_url}/{channel['stream_id']}/index.m3u8?token={TOKEN}"
            
            entry = (
                f'#EXTINF:-1 tvg-name="{channel["name"]}" '
                f'tvg-logo="https://logo.doube.eu.org/{safe_name}.png" '
                f'group-title="{channel["group"]}",{channel["name"]}\n'
                f"{stream_url}"
            )
            output.append(entry)
    
    #return Response('\n'.join(output), mimetype='application/vnd.apple.mpegurl')
    return Response('\n'.join(output), mimetype='text/plain')


@app.route('/<file_prefix>', methods=['GET', 'POST'])
def convert_txt_to_json(file_prefix):
    # Token验证
    if request.args.get('token') != TOKEN:
        abort(403, description="Invalid token")

    # 处理写入确认
    if request.method == 'POST':
        if not request.json.get('confirm'):
            return '操作已取消', 200
        
        try:
            # 重新读取数据确保一致性
            converted_data = convert_file(file_prefix)
            with config_lock:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(converted_data, f, ensure_ascii=False, indent=2)
                global last_modified
                last_modified = os.path.getmtime(CONFIG_PATH)
                load_config()
            return jsonify({"status": "success", "message": "配置文件已更新"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # GET请求处理
    try:
        converted_data = convert_file(file_prefix)
        
        # 生成预览页面
        preview_html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>配置预览</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                pre {{ background: #f5f5f5; padding: 15px; border-radius: 5px; }}
                .buttons {{ margin-top: 20px; }}
                button {{ padding: 10px 20px; margin-right: 10px; cursor: pointer; }}
            </style>
        </head>
        <body>
            <h2>转换预览</h2>
            
            <div class="buttons">
                <button onclick="confirmWrite(true)">确认写入（会覆盖stream.json）</button>
                <button onclick="confirmWrite(false)">取消</button>
            </div>
            <pre>{html.escape(json.dumps(converted_data, ensure_ascii=False, indent=2))}</pre>
            <script>
                function confirmWrite(confirm) {{
                    fetch(window.location.href, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ confirm: confirm }})
                    }})
                    .then(response => response.json())
                    .then(data => {{
                        alert(data.message);
                        if(data.status === 'success') window.close();
                    }});
                }}
            </script>
        </body>
        </html>
        '''
        return render_template_string(preview_html)

    except FileNotFoundError:
        abort(404, description=f"{file_prefix}.txt not found")
    except Exception as e:
        app.logger.error(f"转换失败: {str(e)}")
        abort(500, description=f"转换错误: {str(e)}")

def convert_file(file_prefix):
    """通用转换函数"""
    config_dir = os.path.dirname(CONFIG_PATH)
    txt_path = os.path.join(config_dir, f"{file_prefix}.txt")
    
    channels = []
    current_group = "默认分组"
    channel_counter = 1
    
    with open(txt_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            if '#genre#' in line:
                current_group = line.split(',#genre#')[0].strip()
                continue
            
            if ',' in line:
                name, url = line.split(',', 1)
                channels.append({
                    "group": current_group,
                    "name": name.strip(),
                    "url": url.strip(),
                    "stream_id": f"channel{channel_counter}"
                })
                channel_counter += 1

    return {
        "token": config_data.get('token', TOKEN),
        "hls_time": config_data.get('hls_time', '3'),
        "hls_list_size": config_data.get('hls_list_size', '12'),
        "list": channels
    }

@app.route('/help')
def show_help():
    help_text = """by 公众号【医工学习日志】

iptv-hls v1.0
支持amd64 / ARM64 和 ARMv7

================================

拉取镜像，运行容器：
docker run -d \\
  --name iptv-hls \\
  -p 50086:50086 \\
  -v /etc/docker/iptv-hls/config:/app/config \\
  -v /etc/docker/iptv-hls/hls:/hls \\
  --restart always \\
  cqshushu/iptv-hls:latest

参数说明：
--name   容器名称（可修改）
-p       [主机端口]:50086（第一个端口可自定义）
/etc/docker/iptv-hls 配置文件存放路径（可修改自己的路径）

================================

接口说明：
1. TXT转JSON配置（需确认）：
   http://[ip]:[port]/[txt文件名]?token=[token]
   示例：http://192.168.1.100:50086/channels-to-json?token=my_token

2. 频道列表(TXT格式)：
   http://[ip]:[port]/txt?token=[token]

3. 频道列表(M3U格式)：
   http://[ip]:[port]/m3u?token=[token]

4. 流媒体地址：
   http://[ip]:[port]/[stream_id]/index.m3u8?token=[token]

================================

配置文件路径：
├─ config/
│  ├─ streams.json  # 推流配置
│  └─ *.txt        # 频道源文件
└─ hls/            # 实时生成的HLS分片"""
    
    return Response(help_text, mimetype='text/plain')

if __name__ == '__main__':
    load_config()
    threading.Thread(target=config_watcher, daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=50086, threaded=True)
