import json
import os
import subprocess
import shutil
import threading
import time
from datetime import datetime
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote
import urllib.parse
from flask import Flask, request, abort, send_from_directory, Response, jsonify
from flask import render_template_string,render_template,make_response
from functools import wraps
import html
import random
import string
import re
from pathlib import Path
from datetime import datetime, timedelta



app = Flask(__name__)

# 配置参数
CONFIG_PATH = '/app/config/streams.json'
PASS_PATH = '/app/config/password.json'
HLS_ROOT = '/app/hls'
CHECK_INTERVAL = 5
STREAM_START_TIMEOUT = 10

# 全局变量
config_lock = threading.Lock()
streams_lock = threading.Lock()
last_modified = 0
TOKEN = "iptv"
config_data = {}
STREAMS = {}  # 结构：{stream_id: {url, always_on, stop_delay, t}}
active_streams = {}

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    return response


# 250417
def password_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 获取当前有效密码
        current_password = get_current_password()
        
        # 从请求参数获取密码
        submitted_password = request.args.get('password')
        
        if not submitted_password or submitted_password != current_password:
            abort(403, description="Invalid password")
        return f(*args, **kwargs)
    return decorated

def get_current_password():
    """获取当前有效密码"""
    default_password = "iptv1234"
    try:
        if os.path.exists(PASS_PATH):
            with open(PASS_PATH, 'r') as f:
                data = json.load(f)
                return data.get('password', default_password)
        return default_password
    except:
        return default_password

#250417

# 添加自定义JSON编码器
class OrderedJSONEncoder(json.JSONEncoder):
    def encode(self, o):
        def handle_dict(obj):
            if isinstance(obj, OrderedDict):
                return {k: handle_dict(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [handle_dict(e) for e in obj]
            return obj
        
        # 手动控制序列化过程
        return super().encode(handle_dict(o))

app.json_encoder = OrderedJSONEncoder  # 应用自定义编码器

class StreamProcess:
    def __init__(self, stream_id, url, always_on, stop_delay, *args, **kwargs):
        self.stream_id = stream_id
        self.url = url
        self.always_on = always_on
        self.stop_delay = stop_delay
        self.process = None
        self.start_time = datetime.now()
        self.last_access = datetime.now()
        self.output_dir = Path(HLS_ROOT) / stream_id
        self.running = False
        self.ready_event = threading.Event()

    def start(self):
        try:
            # ==================== 初始化输出目录 ====================
            shutil.rmtree(self.output_dir, ignore_errors=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            
            # 验证目录可写性
            test_file = self.output_dir / "write_test.tmp"
            try:
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
            except Exception as e:
                raise RuntimeError(f"输出目录不可写: {str(e)}")

            # ==================== 构建FFmpeg命令 ====================
            with config_lock:  # 确保配置读取线程安全
                hls_time = config_data.get('hls_time', '3')
                hls_list_size = config_data.get('hls_list_size', '12')
                video_codec = config_data.get('video_codec', 'copy') #250420
                audio_codec = config_data.get('audio_codec', 'copy') #250420

            cmd = [
                'ffmpeg',
                '-i', self.url,
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-analyzeduration', '1000000',
                '-probesize', '1000000',
                '-c:v', video_codec,
                '-c:a', audio_codec,
                '-f', 'hls',
                '-hls_time', hls_time,
                '-hls_list_size', hls_list_size,
                '-hls_flags', 'delete_segments+append_list+temp_file',
                '-start_number', '0',
                '-hls_base_url', f'/tsfile/live/{self.stream_id}/',
                str(self.output_dir / 'index.m3u8')
            ]


            # ==================== 日志管理 ==================== 
            # 生成带日期的日志路径
            log_dir = Path("/app/logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            current_date = datetime.now().strftime("%Y%m%d")
            ffmpeg_log_path = log_dir / f"ffmpeg_{self.stream_id}_{current_date}.log"
            
            # 清理过期日志（保留最近2天）
            self._clean_old_logs(log_dir)

            # ==================== 启动进程 ====================
            with open(ffmpeg_log_path, "w") as log_file:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    # 确保子进程独立于主进程（防止僵尸进程）
                    start_new_session=True  
                )
            
            self.running = True
            app.logger.info(f"视频流[{self.stream_id}]启动成功 PID:{self.process.pid}")
            
            # ==================== 启动监控线程 ====================
            threading.Thread(
                target=self._monitor_ready, 
                daemon=True,
                name=f"StreamMonitor-{self.stream_id}"  # 线程命名便于调试
            ).start()

        except Exception as e:
            app.logger.error(f"视频流[{self.stream_id}]启动失败: {str(e)}", exc_info=True)
            self.running = False
            self.ready_event.set()  # 防止请求线程死锁

    def _clean_old_logs(self, log_dir):
        """清理超过2天的日志文件"""
        cutoff = datetime.now() - timedelta(days=2)
        pattern = re.compile(rf"ffmpeg_{self.stream_id}_(\d{{8}})\.log$")
        
        for log_file in log_dir.iterdir():
            if not log_file.is_file():
                continue
                
            # 匹配当前流ID的日志文件
            match = pattern.match(log_file.name)
            if not match:
                continue
                
            try:
                # 解析文件日期
                file_date = datetime.strptime(match.group(1), "%Y%m%d")
                if file_date < cutoff:
                    log_file.unlink()
                    app.logger.debug(f"清理过期日志: {log_file}")
            except Exception as e:
                app.logger.warning(f"日志清理失败 {log_file}: {str(e)}")

    def _monitor_ready(self):
        start_time = time.time()
        app.logger.info(f"开始监控流 {self.stream_id} 的初始化状态")
        
        while time.time() - start_time < STREAM_START_TIMEOUT:
            # 检查进程是否已退出（失败）
            if self.process.poll() is not None:
                app.logger.error(f"FFmpeg进程意外退出，返回码: {self.process.returncode}")
                self.stop()
                break
            
            # 检查目标文件是否存在且非空
            m3u8_path = self.output_dir / 'index.m3u8'
            if m3u8_path.exists() and os.path.getsize(m3u8_path) > 0:
                app.logger.info(f"检测到有效 index.m3u8 文件， [{self.stream_id}] 视频流准备就绪")
                self.ready_event.set()
                return
            
            time.sleep(1)  # 检查间隔改为1秒
        
        # 超时处理
        app.logger.error(f"{self.stream_id} 视频流初始化超时（{STREAM_START_TIMEOUT}秒未就绪）")
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
        app.logger.info(f"{self.stream_id} 视频流关闭")

def load_config():
    global last_modified, TOKEN, STREAMS, config_data, logo_path
    try:
        current_modified = os.path.getmtime(CONFIG_PATH)
        #print("当前：",current_modified)
        if current_modified == last_modified:
            #print("无需修改")
            return

        with config_lock:
            with open(CONFIG_PATH) as f:
                config_data = json.load(f)
                new_token = config_data['token']
                logo_path = config_data['logo_path']
                video_codec = config_data['video_codec'] #250420
                audio_codec = config_data['audio_codec'] #250420
                new_streams = {}


                for s in config_data['list']:
                    stream_id = s['stream_id']
                    t_values = s.get('t', [])
                    if isinstance(t_values, str):
                        t_values = [t_values]
                    
                    new_streams[stream_id] = {
                        'name': s.get('name', stream_id),  # 新增name字段，默认值为stream_id
                        'url': s['url'],
                        'always_on': int(s.get('always_on', 0)),
                        'stop_delay': int(s.get('stop_delay', 30)),
                        't': t_values
                    }

            TOKEN = new_token
            STREAMS = new_streams.copy()
            last_modified = current_modified

        # 更新已有流的参数
        with streams_lock:
            for stream_id, sp in active_streams.items():
                if stream_id in new_streams:
                    new_params = new_streams[stream_id]
                    sp.always_on = new_params['always_on']
                    sp.stop_delay = new_params['stop_delay']

    except Exception as e:
        app.logger.error(f"Error loading config: {str(e)}")

"""
def cleanup_worker():
    while True:
        time.sleep(10)
        now = datetime.now()
        with streams_lock:
            to_remove = []
            for stream_id, sp in active_streams.items():
                # 直接读取实例属性（已动态更新）
                always_on = sp.always_on
                stop_delay = sp.stop_delay

                if always_on == 1:
                    if stop_delay == 0:
                        continue
                    else:
                        start_elapsed = (now - sp.start_time).total_seconds()
                        access_elapsed = (now - sp.last_access).total_seconds()
                        timeout = (start_elapsed > stop_delay) or (access_elapsed > stop_delay)
                else:
                    if stop_delay == 0:
                        timeout = False
                    else:
                        timeout = (now - sp.last_access).total_seconds() > stop_delay

                if timeout:
                    sp.stop()
                    to_remove.append(stream_id)
            
            for sid in to_remove:
                del active_streams[sid]
"""

def cleanup_worker():
    while True:
        time.sleep(10)
        now = datetime.now()
        with streams_lock:
            to_remove = []
            for stream_id, sp in active_streams.items():
                # 确保从配置读取最新参数
                config = STREAMS.get(stream_id, {})
                always_on = config.get('always_on', 0)
                stop_delay = config.get('stop_delay', 30)

                # 统一使用整数秒数判断
                access_elapsed = (now - sp.last_access).total_seconds()

                if always_on == 1:
                    if stop_delay == 0:
                        continue
                    else:
                        start_elapsed = (now - sp.start_time).total_seconds()
                        access_elapsed = (now - sp.last_access).total_seconds()
                        timeout = (start_elapsed > stop_delay) or (access_elapsed > stop_delay)
                else:
                    if stop_delay == 0:
                        timeout = False
                    else:
                        timeout = (now - sp.last_access).total_seconds() > stop_delay

                if timeout:
                    app.logger.info(f" {stream_id} 流超时关闭 (最后访问: {access_elapsed:.1f}秒)")
                    sp.stop()
                    to_remove.append(stream_id)
            
            for sid in to_remove:
                del active_streams[sid]


def config_watcher():
    while True:
        load_config()
        time.sleep(CHECK_INTERVAL)

"""
@app.before_request
def handle_request():
    path = request.path.strip('/').split('/')
    if len(path) > 0 and path[0] in STREAMS:
        stream_id = path[0]
        with streams_lock:
            if stream_id in active_streams:
                active_streams[stream_id].last_access = datetime.now()
"""

@app.before_request
def handle_request():
    # 提取stream_id的新逻辑
    if request.path.startswith('/tsfile/live/'):
        parts = request.path.strip('/').split('/')
        if len(parts) >= 3:
            # 处理两种路径格式：
            # /tsfile/live/stream_id.m3u8
            # /tsfile/live/stream_id/filename.ts
            stream_id_part = parts[2]
            stream_id = stream_id_part.split('.')[0]  # 去除扩展名
            
            with streams_lock:
                if stream_id in active_streams:
                    active_streams[stream_id].last_access = datetime.now()


@app.route('/')
def index():
    return render_template('index.html', current_host=request.host)


#@app.route('/<stream_id>/index.m3u8')
#def serve_hls(stream_id):
    #t_param = request.args.get('t')
@app.route('/tsfile/live/<stream_id>.m3u8')
def serve_hls(stream_id):
    t_param = request.args.get('key')  # 修改参数名为key
    stream_info = STREAMS.get(stream_id)
    
    if not stream_info or t_param not in stream_info['t']:
        abort(403, description="无效节目token")

    with streams_lock:
        sp = active_streams.get(stream_id)
        
        if not sp or not sp.running:
            sp = StreamProcess(
                stream_id=stream_id,
                url=stream_info['url'],
                always_on=stream_info['always_on'],
                stop_delay=stream_info['stop_delay']
            )
            sp.start()
            if sp.running:
                active_streams[stream_id] = sp
            else:
                abort(503, description="视频流启动失败")

        if not sp.ready_event.wait(timeout=10):
            abort(503, description="视频流初始化超时，请检查原始视频流是否正常")

    m3u8_path = sp.output_dir / 'index.m3u8'
    with open(m3u8_path, 'r') as f:
        content = f.read()
    #modified_content = content.replace('.ts', f'.ts?t={t_param}')
    modified_content = content.replace('.ts', f'.ts?key={t_param}')
    
    return Response(modified_content, mimetype='application/vnd.apple.mpegurl')

#@app.route('/<stream_id>/<filename>')
#def serve_ts(stream_id, filename):
    #t_param = request.args.get('t')
@app.route('/tsfile/live/<stream_id>/<filename>')
def serve_ts(stream_id, filename):
    t_param = request.args.get('key')  # 修改参数名为key
    stream_info = STREAMS.get(stream_id)
    
    if not stream_info or t_param not in stream_info['t']:
        abort(403, description="Invalid t parameter")
    
    base_filename = filename.split('?')[0]
    stream_dir = Path(HLS_ROOT) / stream_id
    file_path = stream_dir / base_filename
    
    if not file_path.exists():
        abort(404, description="Segment not found")
    
    return send_from_directory(str(stream_dir), base_filename)


@app.route('/txt')
def channel_list():
    #if request.args.get('token') != TOKEN:
        #abort(403, description="Invalid token")
    if request.args.get('token') == TOKEN:
        base_url = request.host_url.rstrip('/')
        output = []
        groups = {}
        current_group = None
        
        with config_lock:
            for channel in config_data['list']:
                group_name = channel.get('group', '未分组')
                if group_name != current_group:
                    output.append(f"{group_name},#genre#")
                    current_group = group_name
                    groups.setdefault(group_name, [])
                
                for t_value in channel.get('t', []):
                    #stream_url = f"{base_url}/{channel['stream_id']}/index.m3u8?t={t_value}"
                    stream_url = f"{base_url}/tsfile/live/{channel['stream_id']}.m3u8?key={t_value}"
                    groups[group_name].append(f"{channel['name']},{stream_url}")

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
    else:
        # 返回包含JavaScript的响应以提示输入Token
        return '''
            <script>
                // 弹出输入框让用户输入Token
                let userToken = prompt("请输入TOKEN:");
                
                if (userToken !== null) {
                    // 用户输入后重定向到带有Token的同一页面
                    window.location.href = '/txt?token=' + encodeURIComponent(userToken);
                } else {
                    // 用户取消输入则尝试关闭窗口
                    alert('访问被拒绝，窗口即将关闭。');
                    window.close();
                }
            </script>
        '''


@app.route('/m3u')
def generate_m3u():
    if request.args.get('token') == TOKEN:
        #abort(403, description="Invalid token")

        base_url = request.host_url.rstrip('/')
        output = ["#EXTM3U"]
        
        with config_lock:
            for channel in config_data['list']:
                safe_name = quote(channel['name'].strip().replace(' ','_'), safe='')
                for t_value in channel.get('t', []):
                    #stream_url = f"{base_url}/{channel['stream_id']}/index.m3u8?t={t_value}"
                    stream_url = f"{base_url}/tsfile/live/{channel['stream_id']}.m3u8?key={t_value}"
                    entry = (
                        f'#EXTINF:-1 tvg-name="{channel["name"]}" '
                        f'{logo_path}{safe_name}.png" '
                        f'group-title="{channel["group"]}",{channel["name"]}\n'
                        f"{stream_url}"
                    )
                    output.append(entry)
        
        #return Response('\n'.join(output), mimetype='application/vnd.apple.mpegurl')
        return Response('\n'.join(output), mimetype='text/plain')

    else:
        # 返回包含JavaScript的响应以提示输入Token
        return '''
            <script>
                // 弹出输入框让用户输入Token
                let userToken = prompt("请输入TOKEN:");
                
                if (userToken !== null) {
                    // 用户输入后重定向到带有Token的同一页面
                    window.location.href = '/m3u?token=' + encodeURIComponent(userToken);
                } else {
                    // 用户取消输入则尝试关闭窗口
                    alert('访问被拒绝，窗口即将关闭。');
                    window.close();
                }
            </script>
        '''

@app.route('/tool', methods=['GET'])
def password_protected_page():
    # 尝试从password.json读取密码
    default_password = "iptv1234"
    current_password = default_password
    
    try:
        if os.path.exists(PASS_PATH):
            with open(PASS_PATH, 'r', encoding='utf-8') as f:
                password_data = json.load(f)
                current_password = password_data.get('password', default_password)
    except Exception as e:
        app.logger.error(f"密码文件读取失败: {str(e)}")

    # 获取用户提交的密码
    submitted_password = request.args.get('password')
    
    # 验证密码
    if submitted_password == current_password:
        return render_template('tool.html')
    else:
        # 密码验证失败时弹出密码输入框
        return '''
            <script>
                function validatePassword() {
                    const pass = prompt("请输入访问密码:");
                    if(pass !== null) {
                        window.location.href = `/tool?password=${encodeURIComponent(pass)}`;
                    } else {
                        if(window.history.length > 1) {
                            window.history.back();
                        } else {
                            window.close();
                        }
                    }
                }
                
                // 首次加载立即验证
                validatePassword();
                
                // 防止浏览器后退绕过验证
                window.addEventListener('popstate', function() {
                    validatePassword();
                });
            </script>
        '''

@app.route('/convert', methods=['POST'])
@password_required
def convert_txt():
    # Token验证
    #token = request.args.get('token')
    #if token != TOKEN:
        #abort(403, description="Invalid token")
    
    try:
        content = request.get_data(as_text=True)
        converted_data = convert_file_content(content)

        # 手动序列化（关键修改）
        json_str = json.dumps(
            converted_data,
            cls=OrderedJSONEncoder,
            ensure_ascii=False,  # 禁止Unicode转义
            indent=2
        )

        # 直接返回原始JSON字符串
        response = make_response(json_str)
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response

        #print("原始JSON序列化结果:", json.dumps(converted_data, cls=OrderedJSONEncoder, indent=2))
        #return jsonify({"status": "success", "data": converted_data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/write_config', methods=['POST'])
@password_required
def write_config():
    # Token验证
    #token = request.args.get('token')
    #if token != TOKEN:
        #abort(403, description="Invalid token")
    
    data = request.get_json()
    if not data.get('confirm'):
        return jsonify({"status": "cancel", "message": "操作已取消"}), 200
    
    try:
        json_content = data.get('content')
        with config_lock:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(json_content, f, ensure_ascii=False, indent=2)
        global last_modified
        #last_modified = os.path.getmtime(CONFIG_PATH)
        last_modified = 0
        load_config()
        return jsonify({"status": "success", "message": "配置文件已更新"})
    except Exception as e:
        app.logger.error(f"写入失败: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# 修改后的转换函数
def convert_file_content(content):
    channels = []
    current_group = "默认分组"
    channel_counter = 1001  # 从1001开始的全局计数器
    
    def generate_random_string(length=8):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    
    for line in content.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        if '#genre#' in line:
            current_group = line.split(',#genre#')[0].strip()
            continue
        
        if ',' in line:
            name, url = line.split(',', 1)
            # 生成纯数字递增ID
            stream_id = f"{str(channel_counter)}_1"
            channel_counter += 1  # 无限制递增
            
            channel = OrderedDict([
                ("group", current_group),
                ("name", name.strip()),
                ("url", url.strip()),
                ("stream_id", stream_id),
                ("always_on", 0),
                ("stop_delay", 30),
                ("t", [generate_random_string()])
            ])
            channels.append(channel)

    return OrderedDict([
        ("token", config_data.get('token', TOKEN)),
        ("video_codec", config_data.get('video_codec', 'copy')),
        ("audio_codec", config_data.get('audio_codec', 'copy')),
        ("hls_time", config_data.get('hls_time', '3')),        
        ("hls_list_size", config_data.get('hls_list_size', '12')),
        ("logo_path", config_data.get('logo_path', 'https://live.fanmingming.com/tv/')),
        ("list", channels)
    ])

#载入配置streams.json
@app.route('/load_default', methods=['GET'])
@password_required
def load_default():
    
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config_content = json.load(f, object_pairs_hook=OrderedDict)
        
        # 保持顺序的序列化
        json_str = json.dumps(
            config_content,
            cls=OrderedJSONEncoder,
            ensure_ascii=False,
            indent=2
        )
        response = make_response(json_str)
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
        
    except FileNotFoundError:
        abort(404, description="配置文件不存在")
    except Exception as e:
        app.logger.error(f"配置读取失败: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# 保存password到password.json
@app.route('/save_password', methods=['POST'])
@password_required
def save_password():
    try:
        new_password = request.json.get('password')

        # 仅保留基本保存逻辑
        password_data = {"password": new_password}

        with config_lock:
            with open(PASS_PATH, 'w', encoding='utf-8') as f:
                json.dump(password_data, f, ensure_ascii=False, indent=2)
                
            return jsonify({"status": "success", "message": "密码已更新"})
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
# 读取配置token
@app.route('/get_config_token', methods=['GET'])
@password_required
def get_config_token():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return jsonify({"status": "success", "token": config.get('token', '')})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



@app.route('/player')
def player():
    # Token验证
    token = request.args.get('token')
    if token == TOKEN:
        
        
        # 获取频道列表数据
        channels = []
        with config_lock:
            for channel in config_data.get('list', []):
                tokens = channel.get('t', [])
                valid_tokens = [t for t in (tokens if isinstance(tokens, list) else [tokens]) if t.strip()]
                if valid_tokens:
                    channels.append({
                        'name': channel['name'],
                        'stream_id': channel['stream_id'],
                        'token': valid_tokens[0],
                        'group': channel.get('group', '其他')
                    })
        return render_template('player.html', channels=channels)

    else:
        # 返回包含JavaScript的响应以提示输入Token
        return '''
            <script>
                // 弹出输入框让用户输入Token
                let userToken = prompt("请输入TOKEN:");
                
                if (userToken !== null) {
                    // 用户输入后重定向到带有Token的同一页面
                    window.location.href = '/player?token=' + encodeURIComponent(userToken);
                } else {
                    // 用户取消输入则尝试关闭窗口
                    alert('访问被拒绝，窗口即将关闭。');
                    window.close();
                }
            </script>
        '''



# 原最后部分改为
def main():
    load_config()
    
    with streams_lock:
        for stream_id, stream_info in STREAMS.items():
            if stream_info['always_on'] == 1:
                sp = StreamProcess(
                    stream_id=stream_id,
                    url=stream_info['url'],
                    always_on=stream_info['always_on'],
                    stop_delay=stream_info['stop_delay']
                )
                sp.start()
                active_streams[stream_id] = sp

    threading.Thread(target=config_watcher, daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=50086, threaded=True)

if __name__ == '__main__':
    main()
