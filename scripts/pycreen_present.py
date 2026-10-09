# AI生成
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pycreen_present.py — screen 会话监控服务

功能:
  1. 查看服务列表: 返回会话编号 + 名称
  2. 服务 +编号: 返回指定会话的子进程详细信息(不带编号则不查看)

@指令(普通权限也可用, 关键词可在配置区修改):
  @机器人 查看服务列表          返回会话编号 + 名称
  @机器人 服务 编号             返回指定会话详细进程信息

用法:
  python3 pycreen_present.py               常驻(WS 监听)
  python3 pycreen_present.py --test        单次测试业务逻辑
  python3 pycreen_present.py --list        打印当前状态
  python3 pycreen_present.py --check       只检查依赖/配置

依赖: psutil (第三方), 其余纯标准库
"""

import os
import re
import sys
import json
import time
import socket
import base64
import struct
import logging
import argparse
import subprocess
import threading
import urllib.request

# Linux 专用模块, Windows 下优雅降级
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ============ 内存限制(纯逻辑脚本, 无浏览器) ============
if HAS_RESOURCE:
    MEM_LIMIT_MB = 48   # 纯逻辑+psutil, 48MB 足够(参考 spark_status=64MB)
    try:
        resource.setrlimit(resource.RLIMIT_AS,
                          (MEM_LIMIT_MB * 1024 * 1024, MEM_LIMIT_MB * 1024 * 1024))
    except Exception:
        pass

# ============ 目录结构(全项目统一约定) ============
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))   # scripts/
ROOT_DIR  = os.path.dirname(BASE_DIR)                    # 项目根
CONF_DIR  = os.path.join(ROOT_DIR, "conf")               # 配置/状态 json
LOG_DIR   = os.path.join(ROOT_DIR, "logs")               # 日志
IMAGE_DIR = os.path.join(ROOT_DIR, "images")             # 图片/截图

for _d in (CONF_DIR, LOG_DIR, IMAGE_DIR):
    os.makedirs(_d, exist_ok=True)

# ============ 配置区(按需修改) ============
WS_HOST, WS_PORT, WS_TOKEN = "127.0.0.1", 3001, "napcat"
HTTP_BASE, HTTP_TOKEN      = "http://127.0.0.1:3000", "napcat"
BOT_QQ      = 1000000001
SUPER_ADMIN = 1000000002
ALLOW_GROUPS = []

LOCK_FILE   = os.path.join(BASE_DIR, ".pycreen_present.lock")
LOG_FILE    = os.path.join(LOG_DIR, "pycreen_present.log")
ADMIN_FILE  = os.path.join(CONF_DIR, "admins.json")
STATE_FILE  = os.path.join(CONF_DIR, "pycreen_present.json")

# 仅保留两条指令(普通权限也可用):
#   查看服务列表  → 返回会话编号 + 名称
#   服务 编号     → 返回指定会话的子进程详情(不带编号则不查看)
# ⚠️ "查看服务列表"包含"服务", handle_command 里必须先判断列表再判断详情
KW_VIEW_LIST   = ["查看服务列表"]
KW_VIEW_DETAIL = ["服务"]

REPLY_DELAY    = 1.0
MAX_MSG_LEN    = 3500
QUERY_INTERVAL = 10   # 查询间隔(秒)
# =========================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("pycreen_present")


# ══════════════════════════════════════════════════════════════════
#  公共方法库 (6 个区块, 从模板复制)
# ══════════════════════════════════════════════════════════════════

# ─────────────── 区块1: 权限判断 ───────────────
def load_admins():
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f).get("admins", [])}
    except Exception:
        return set()


def is_privileged(qq):
    return int(qq) == int(SUPER_ADMIN) or int(qq) in load_admins()


# ─────────────── 区块2: JSON 读写 ───────────────
def json_load(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}
    except Exception as e:
        log.info(f"[读 {os.path.basename(path)} 失败] {e}")
        return default if default is not None else {}


def json_save(path, data):
    lock = None
    if HAS_FCNTL:
        lock = open(path + ".lock", "w")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


# ─────────────── 区块3: NapCat HTTP API ───────────────
def api(action, payload=None):
    req = urllib.request.Request(
        f"{HTTP_BASE}/{action}",
        data=json.dumps(payload or {}).encode(),
        headers={"Authorization": f"Bearer {HTTP_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def send_group(group_id, text):
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": text})
    except Exception as e:
        log.info(f"[QQ发送失败] {e}")


def send_group_at(group_id, qq, text):
    msg = [{"type": "at", "data": {"qq": str(qq)}},
           {"type": "text", "data": {"text": " " + text}}]
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": msg})
    except Exception as e:
        log.info(f"[QQ发送失败] {e}")


def send_group_image(group_id, img, text=""):
    msg = [{"type": "image", "data": {"file": img, "cache": "1"}}]
    if text:
        msg.append({"type": "text", "data": {"text": "\n" + text}})
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": msg})
    except Exception as e:
        log.info(f"[QQ发送失败] {e}")


# ─────────────── 区块4: WebSocket 客户端 ───────────────
class WSClient:
    def __init__(self, host, port, token):
        self.host, self.port, self.token = host, port, token
        self.sock, self._buf = None, b""

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            "GET / HTTP/1.1", f"Host: {self.host}:{self.port}",
            "Upgrade: websocket", "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
            f"Authorization: Bearer {self.token}"]
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("握手时连接关闭")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WS 握手失败: {resp[:120]!r}")
        # 300s 无任何数据视为死连(NapCat 心跳约5s, 正常连接不会触发) → 抛超时触发重连
        self.sock.settimeout(300)

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("连接关闭")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def recv(self):
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else b""
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("收到 close 帧")
            elif opcode == 0x9:
                self._send(0xA, payload)
            elif opcode == 0xA:
                continue
            elif fin:
                return payload.decode("utf-8", "replace")

    def _send(self, opcode, data=b""):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        header += mask
        self.sock.sendall(header + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def close(self):
        try:
            self._send(0x8)
            self.sock.close()
        except Exception:
            pass


# ─────────────── 区块5: 消息解析 ───────────────
def extract_message(msg_field):
    parts, at_self = [], False
    if isinstance(msg_field, list):
        for seg in msg_field:
            t, d = seg.get("type"), seg.get("data", {})
            if t == "text":
                parts.append(d.get("text", ""))
            elif t == "at" and str(d.get("qq")) == str(BOT_QQ):
                at_self = True
    elif isinstance(msg_field, str):
        parts.append(msg_field)
        at_self = f"[CQ:at,qq={BOT_QQ}]" in msg_field
    return "".join(parts).strip(), at_self


# ─────────────── 区块6: 单实例锁 ───────────────
_LOCK_FP = None   # 全局持有锁文件句柄, 防止 GC 释放导致锁丢失

def acquire_lock():
    global _LOCK_FP
    if not HAS_FCNTL:
        return True
    _LOCK_FP = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_LOCK_FP.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FP.write(str(os.getpid()))
        _LOCK_FP.flush()
        return _LOCK_FP
    except BlockingIOError:
        _LOCK_FP = None
        return None


# ══════════════════════════════════════════════════════════════════
#  业务逻辑: screen 会话监控
# ══════════════════════════════════════════════════════════════════

PATTERN = re.compile(
    r'(?:There is a screen on:|There are screens on:)?'
    r'[ \t]*(\d+)\.([^\s(]+)'
    r'[ \t]*(?:\(([^)]*)\))?'
    r'[ \t]*\((\w+)\)'
)

_last_query_time = {}


def check_rate_limit(cmd_name):
    """检查是否距上次查询已过 QUERY_INTERVAL 秒。"""
    now = time.time()
    elapsed = now - _last_query_time.get(cmd_name, 0)
    if elapsed < QUERY_INTERVAL:
        return False, int(QUERY_INTERVAL - elapsed)
    _last_query_time[cmd_name] = now
    return True, 0


HELP_TEXT = (
    "🔧 screen 服务监控用法\n"
    "查看服务列表            返回会话编号 + 名称\n"
    "服务 编号               返回指定会话的子进程详细信息\n"
    "(不带编号则不查看)"
)


def sub_screen_ls():
    """执行 screen -ls, 解析返回 (编号->PID 映射, 名称列表, 时间列表, 状态列表)。
    screen 未安装时返回空列表(调用方显示「没有 screen 会话」)。"""
    try:
        sub_screen = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=5,
        )
    except FileNotFoundError:
        log.info("[screen] 命令未安装")
        return {}, [], [], []
    except subprocess.TimeoutExpired:
        log.info("[screen] 命令超时")
        return {}, [], [], []
    name_PId = []
    name_pid_name = []
    name_Time = []
    name_status = []
    for m in PATTERN.finditer(sub_screen.stdout):
        name_PId.append(m.group(1))
        name_pid_name.append(m.group(2))
        name_Time.append(m.group(3))
        name_status.append(m.group(4))
    mapping_id = {i: pid for i, pid in enumerate(name_PId, start=1)}
    return mapping_id, name_pid_name, name_Time, name_status


def get_child_processes(screen_pid):
    """返回该 screen 会话下所有子进程的 psutil.Process 对象列表。"""
    if not HAS_PSUTIL:
        return []
    procs = []
    try:
        parent = psutil.Process(int(screen_pid))
        procs = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return procs


def describe_process(proc):
    """获取单个进程的详细信息。"""
    if not HAS_PSUTIL:
        return {"error": "psutil 未安装"}
    try:
        with proc.oneshot():
            # ---- 服务类型: 按解释器/程序归类(sh/python/java/node/其他) ----
            first_tok = (proc.cmdline() or [""])[0]
            exe_base = os.path.basename(first_tok)
            if exe_base in ("bash", "sh", "dash", "zsh"):
                svc_type = "sh"
            elif exe_base.startswith("python"):
                svc_type = "python"
            elif exe_base == "java":
                svc_type = "java"
            elif exe_base == "node":
                svc_type = "node"
            else:
                svc_type = exe_base or "未知"

            # ---- 运行时长: 现在减启动时刻, 格式 x天x小时x分 ----
            ct = proc.create_time()
            elapsed = int(time.time() - ct)
            days, rem = divmod(elapsed, 86400)
            hours, rem = divmod(rem, 3600)
            minutes = rem // 60
            if days > 0:
                uptime_txt = f"{days}天{hours}小时{minutes}分"
            elif hours > 0:
                uptime_txt = f"{hours}小时{minutes}分"
            else:
                uptime_txt = f"{minutes}分钟"

            # ---- 监听端口: 全系统连接表里筛本 PID 的 LISTEN ----
            ports = []
            try:
                for c in psutil.net_connections(kind="inet"):
                    if c.pid == proc.pid and c.status == psutil.CONN_LISTEN \
                            and c.laddr:
                        ports.append(str(c.laddr.port))
            except Exception:
                pass   # 权限不足/平台不支持时静默跳过
            ports = sorted(set(ports), key=int)

            # ---- 状态中文化 ----
            status_cn = {"running": "运行中", "sleeping": "运行中(空闲)",
                         "stopped": "已停止", "zombie": "僵尸进程",
                         "disk-sleep": "运行中(磁盘IO)"}.get(
                             proc.status(), proc.status())

            info = {
                "pid": proc.pid,
                "name": proc.name(),
                "cmdline": " ".join(proc.cmdline()),
                "svc_type": svc_type,
                "status": status_cn,
                "uptime": uptime_txt,
                "start_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(ct)),
                "ports": ",".join(ports) if ports else "无",
                # interval=None 非阻塞(首次返回0.0, 后续返回两次调用间的均值)
                "cpu_percent": proc.cpu_percent(interval=None),
                "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 2),
                "num_threads": proc.num_threads(),
                "cwd": proc.cwd(),
            }
        return info
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
        return {"error": str(e), "pid": proc.pid}


def cmd_view_service_list():
    """查看服务列表: 返回会话编号 + 名称。"""
    mapping_id, names, _, _ = sub_screen_ls()
    if not mapping_id:
        return "📋 当前没有 screen 会话"
    lines = [f"📋 服务列表 (共 {len(mapping_id)} 个)"]
    for idx, pid in mapping_id.items():
        name = names[idx - 1] if idx - 1 < len(names) else "?"
        lines.append(f"  [{idx}] {name}")
    return "\n".join(lines)


def cmd_view_detail(text):
    """服务+编号: 返回指定会话的子进程详细信息。"""
    m = re.search(r"(\d+)", text)
    if not m:
        return None   # 没有编号 → 不查看(静默)
    idx = int(m.group(1))

    mapping_id, names, _, _ = sub_screen_ls()
    if not mapping_id:
        return "📋 当前没有 screen 会话"
    if idx not in mapping_id:
        return f"❌ 编号 {idx} 不存在, 可用范围: 1-{len(mapping_id)}"

    pid = mapping_id[idx]
    name = names[idx - 1] if idx - 1 < len(names) else "?"
    lines = [f"📋 服务 [{idx}] {name} (PID:{pid})"]

    children = get_child_processes(pid)
    if not children:
        lines.append("  (无子进程)")
        return "\n".join(lines)

    for proc in children:
        info = describe_process(proc)
        if "error" in info:
            lines.append(f"  服务类型: ? | 状态: 未知 ({info['error']})")
            continue
        # 按用户要求格式: 类型/状态/运行时间/端口/CPU/MEM
        lines.append(f"  服务类型: {info['svc_type']}")
        lines.append(f"  状态: {info['status']}")
        lines.append(f"  运行时间: {info['uptime']} (自 {info['start_time']})")
        lines.append(f"  端口: {info['ports']}")
        lines.append(f"  CPU: {info['cpu_percent']}% | 内存: {info['memory_mb']}MB | 线程: {info['num_threads']}")
        lines.append(f"  命令: {info['cmdline'][:80]}")
        if len(children) > 1:
            lines.append("  ——" * 6)
    return "\n".join(lines).rstrip("— ")


def handle_command(text):
    """指令路由: 返回回复文本, 或 None=不是本脚本的指令。
    ⚠️ 必须先判断 KW_VIEW_LIST —— "查看服务列表"包含"服务",
    若先判断 KW_VIEW_DETAIL 会被误吞。"""
    # 1. 查看服务列表(普通权限)
    if any(k in text for k in KW_VIEW_LIST):
        ok, wait = check_rate_limit("view_list")
        if not ok:
            return f"⏳ 查询太频繁, 请 {wait} 秒后再试"
        return cmd_view_service_list()

    # 2. 服务 +编号(普通权限, 不带编号→None 静默不查看)
    if any(k in text for k in KW_VIEW_DETAIL):
        # 先检查有没有编号, 没编号直接静默(不消耗限流额度)
        if not re.search(r"\d+", text):
            return None
        ok, wait = check_rate_limit("view_detail")
        if not ok:
            return f"⏳ 查询太频繁, 请 {wait} 秒后再试"
        return cmd_view_detail(text)

    return None


def on_event(ev):
    """WS 事件处理。普通权限也可用, 不检查 is_privileged。"""
    if ev.get("post_type") != "message" or ev.get("message_type") != "group":
        return
    group_id = ev.get("group_id")
    if ALLOW_GROUPS and group_id not in ALLOW_GROUPS:
        return
    qq = ev.get("user_id")
    text, at_self = extract_message(ev.get("message"))
    if not at_self:
        return
    all_kw = KW_VIEW_LIST + KW_VIEW_DETAIL
    if not any(k in text for k in all_kw):
        return
    # 普通权限也可用, 不再做 is_privileged 检查
    try:
        reply = handle_command(text)
    except Exception as e:
        reply = f"❌ 处理失败: {e}"
        log.info(f"[业务异常] {e}")
    if not reply:
        return
    if len(reply) > MAX_MSG_LEN:
        reply = reply[:MAX_MSG_LEN] + "\n...(已截断)"
    time.sleep(REPLY_DELAY)
    send_group(group_id, f"[CQ:at,qq={qq}]\n{reply}")
    log.info(f"群{group_id} {qq}: {text[:30]} -> 已回复")


# ══════════════════════════════════════════════════════════════════
#  主循环与 CLI
# ══════════════════════════════════════════════════════════════════

def ws_loop():
    """WS 主循环, 断线自动重连。"""
    while True:
        ws = WSClient(WS_HOST, WS_PORT, WS_TOKEN)
        try:
            ws.connect()
            log.info(f"[WS] 已连接 NapCat {WS_HOST}:{WS_PORT}")
            while True:
                # ⚠️ recv() 必须在 try 外: 连接层错误(断线/半开)直接抛到外层触发重连;
                # 之前放 try 里被 continue 吞掉, NapCat 重启后脚本假死、永不重连
                raw = ws.recv()
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                try:
                    on_event(ev)
                except Exception as e:
                    log.info(f"[事件处理异常] {e}")
        except Exception as e:
            log.info(f"[WS] 断开: {e}, 5秒重连")
            try:
                ws.close()
            except Exception:
                pass
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="pycreen_present")
    parser.add_argument("--test",  action="store_true", help="单次测试业务逻辑")
    parser.add_argument("--list",  action="store_true", help="打印当前状态")
    parser.add_argument("--check", action="store_true", help="检查依赖/配置")
    args = parser.parse_args()

    if args.test:
        print("=== 业务逻辑测试 ===")
        print("cmd_view_service_list:", cmd_view_service_list())
        print("cmd_view_detail(服务 1):", cmd_view_detail("服务 1"))
        print("cmd_view_detail(服务 无编号):", cmd_view_detail("服务"))
        print("handle_command(查看服务列表):", handle_command("查看服务列表"))
        print("handle_command(服务 1):", handle_command("服务 1"))
        print("handle_command(服务):", handle_command("服务"))
        print("handle_command(未知):", handle_command("乱写的"))
        return

    if args.list:
        print(cmd_view_service_list())
        return

    if args.check:
        print(f"CONF_DIR  : {CONF_DIR}  {'✅' if os.path.isdir(CONF_DIR) else '❌'}")
        print(f"LOG_DIR   : {LOG_DIR}   {'✅' if os.path.isdir(LOG_DIR) else '❌'}")
        print(f"ADMIN_FILE: {ADMIN_FILE} {'✅' if os.path.exists(ADMIN_FILE) else '⚠️不存在'}")
        print(f"psutil    : {'✅' if HAS_PSUTIL else '❌ 未安装'}")
        print(f"fcntl     : {'✅' if HAS_FCNTL else '⚠️ Windows 不可用'}")
        try:
            r = api("get_status", {})
            print(f"NapCat HTTP: ✅ {r}")
        except Exception as e:
            print(f"NapCat HTTP: ❌ {e}")
        return

    if acquire_lock() is None:
        log.info("已有 pycreen_present 实例在运行, 退出")
        return
    log.info("=" * 50)
    log.info(f"pycreen_present 启动 | bot={BOT_QQ}")
    ws_loop()


if __name__ == "__main__":
    main()
