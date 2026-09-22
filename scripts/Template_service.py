#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
your_service.py — 一句话职责说明（不超过 30 字）

功能:
  1. xxx
  2. xxx

@指令(关键词可在配置区修改):
  @机器人 关键词1          说明
  @机器人 关键词1 参数     说明
  @机器人 帮助            查看用法

用法:
  python3 your_service.py               常驻(WS 监听)
  python3 your_service.py --test        单次测试业务逻辑
  python3 your_service.py --list        打印当前状态
  python3 your_service.py --check       只检查依赖/配置

依赖: 纯标准库(或说明第三方库)
"""

import os
import re
import sys
import json
import time
import socket
import base64
import struct
import hashlib
import logging
import argparse
import subprocess
import threading
import urllib.request
import resource

# Linux 专用模块, Windows 下优雅降级
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

# ============ 内存限制(纯逻辑脚本才加; 浏览器/大模型脚本不要加) ============
MEM_LIMIT_MB = 96
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

# 首次运行自动建目录
for _d in (CONF_DIR, LOG_DIR, IMAGE_DIR):
    os.makedirs(_d, exist_ok=True)

# ============ 配置区(按需修改) ============
# NapCat 连接(全项目一致) —— 部署时改成真实值
WS_HOST, WS_PORT, WS_TOKEN = "127.0.0.1", 3001, "napcat"
HTTP_BASE, HTTP_TOKEN      = "http://127.0.0.1:3000", "napcat"
BOT_QQ      = 1000000001        # ← 部署时改成真实机器人 QQ
SUPER_ADMIN = 1000000002        # ← 部署时改成超管 QQ
ALLOW_GROUPS = []                # 空=所有群

# 文件路径(部署时把 your_service 改成你的服务名)
LOCK_FILE   = os.path.join(BASE_DIR, ".your_service.lock")
LOG_FILE    = os.path.join(LOG_DIR, "your_service.log")
ADMIN_FILE  = os.path.join(CONF_DIR, "admins.json")       # 与 listener 共享
STATE_FILE  = os.path.join(CONF_DIR, "your_service.json") # 自己的状态文件

# 关键词(必须与其它脚本不重叠! 用 grep -rn "KW_" scripts/*.py 检查)
KW_MAIN  = ["你的关键词", "别名1", "别名2"]
KW_HELP  = ["帮助"]

# 行为参数
REPLY_DELAY = 1.0                # 统一延迟回复
MAX_MSG_LEN = 3500               # QQ 单条消息安全长度
# =========================================

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("your_service")


# ══════════════════════════════════════════════════════════════════
#  以下 6 个区块是「公共方法库」, 从别的脚本复制过来即可, 无需改动
# ══════════════════════════════════════════════════════════════════

# ─────────────── 区块1: 权限判断 ───────────────
def load_admins():
    """读取 conf/admins.json 里额外管理员列表。"""
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f).get("admins", [])}
    except Exception:
        return set()


def is_privileged(qq):
    """超管或额外管理员。"""
    return int(qq) == int(SUPER_ADMIN) or int(qq) in load_admins()


# ─────────────── 区块2: JSON 读写(带文件锁, 跨进程安全) ───────────────
def json_load(path, default=None):
    """读 JSON, 失败返回 default。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}
    except Exception as e:
        log.info(f"[读 {os.path.basename(path)} 失败] {e}")
        return default if default is not None else {}


def json_save(path, data):
    """原子写 JSON + 文件锁(跨进程安全, fcntl 缺失时退化为纯原子写)。"""
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
    """调用 NapCat OneBot11 HTTP API。"""
    req = urllib.request.Request(
        f"{HTTP_BASE}/{action}",
        data=json.dumps(payload or {}).encode(),
        headers={"Authorization": f"Bearer {HTTP_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def send_group(group_id, text):
    """发纯文本到群。"""
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": text})
    except Exception as e:
        log.info(f"[QQ发送失败] {e}")


def send_group_at(group_id, qq, text):
    """@某人+文本。"""
    msg = [{"type": "at", "data": {"qq": str(qq)}},
           {"type": "text", "data": {"text": " " + text}}]
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": msg})
    except Exception as e:
        log.info(f"[QQ发送失败] {e}")


def send_group_image(group_id, img, text=""):
    """发图片(url 或 file:///绝对路径) + 可选文字。"""
    msg = [{"type": "image", "data": {"file": img, "cache": "1"}}]
    if text:
        msg.append({"type": "text", "data": {"text": "\n" + text}})
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": msg})
    except Exception as e:
        log.info(f"[QQ发送失败] {e}")


# ─────────────── 区块4: WebSocket 客户端(RFC6455) ───────────────
class WSClient:
    """极简 WebSocket 客户端, 仅支持 NapCat 本地明文 ws。"""
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
        self.sock.settimeout(None)

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
    """从 OneBot11 的 message 字段提取 (纯文本, 是否@机器人)。"""
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
    """抢 fcntl 单实例锁; 已有实例则返回 None; fcntl 缺失时返回 True(不限制)。"""
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
#  以下是你自己的业务逻辑(改这里)
# ══════════════════════════════════════════════════════════════════

HELP_TEXT = (
    "🔧 your_service 用法\n"
    "关键词1           功能说明\n"
    "关键词1 参数      功能说明\n"
    "帮助             显示本说明"
)


def cmd_main(text):
    """主功能: 返回要回复的字符串。"""
    # 示例: 解析参数
    m = re.search(r"关键词1\s*(\S+)", text)
    arg = m.group(1) if m else "默认值"
    return f"✅ 收到参数: {arg}"


def cmd_list():
    """列出当前状态。"""
    state = json_load(STATE_FILE, {})
    if not state:
        return "📋 暂无数据"
    lines = [f"📋 共 {len(state)} 条"]
    for k, v in list(state.items())[:20]:
        lines.append(f"• {k}: {v}")
    return "\n".join(lines)


def handle_command(text):
    """指令路由: 返回回复文本, 或 None=不是本脚本的指令。"""
    if any(k in text for k in KW_HELP):
        return HELP_TEXT
    if any(k in text for k in KW_MAIN):
        return cmd_main(text)
    return None


def on_event(ev):
    """WS 事件处理。"""
    if ev.get("post_type") != "message" or ev.get("message_type") != "group":
        return
    group_id = ev.get("group_id")
    if ALLOW_GROUPS and group_id not in ALLOW_GROUPS:
        return
    qq = ev.get("user_id")
    text, at_self = extract_message(ev.get("message"))
    if not at_self:
        return
    if not any(k in text for k in KW_MAIN + KW_HELP):
        return
    if not is_privileged(qq):
        log.info(f"群{group_id} 普通成员 {qq} 尝试使用, 已忽略")
        return
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
                try:
                    ev = json.loads(ws.recv())
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
    parser = argparse.ArgumentParser(description="your_service")
    parser.add_argument("--test",  action="store_true", help="单次测试业务逻辑")
    parser.add_argument("--list",  action="store_true", help="打印当前状态")
    parser.add_argument("--check", action="store_true", help="检查依赖/配置")
    args = parser.parse_args()

    # ---- 测试模式 ----
    if args.test:
        print("=== 业务逻辑测试 ===")
        print("cmd_main:", cmd_main("关键词1 测试参数"))
        print("cmd_list:", cmd_list())
        print("handle_command:", handle_command("关键词1"))
        print("handle_command(未知):", handle_command("乱写的"))
        return

    # ---- 状态查询 ----
    if args.list:
        print(cmd_list())
        return

    # ---- 依赖自检 ----
    if args.check:
        print(f"CONF_DIR  : {CONF_DIR}  {'✅' if os.path.isdir(CONF_DIR) else '❌'}")
        print(f"LOG_DIR   : {LOG_DIR}   {'✅' if os.path.isdir(LOG_DIR) else '❌'}")
        print(f"ADMIN_FILE: {ADMIN_FILE} {'✅' if os.path.exists(ADMIN_FILE) else '⚠️不存在'}")
        try:
            r = api("get_status", {})
            print(f"NapCat HTTP: ✅ {r}")
        except Exception as e:
            print(f"NapCat HTTP: ❌ {e}")
        return

    # ---- 常驻模式 ----
    if acquire_lock() is None:
        log.info("已有 your_service 实例在运行, 退出")
        return
    log.info("=" * 50)
    log.info(f"your_service 启动 | bot={BOT_QQ} 关键词={KW_MAIN}")
    ws_loop()


if __name__ == "__main__":
    main()