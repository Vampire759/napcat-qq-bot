#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_image.py — 「每日一图」独立脚本（QQ群@触发 + HTTP API + 命令行, 无头常驻）

三种用法:
  1) QQ 群: @机器人 每日一图     —— 群里直接收到当天的必应每日一图+图片介绍
  2) HTTP API(供其它程序/cron 调用, 本机监听):
       GET http://127.0.0.1:8095/                 用法说明
       GET http://127.0.0.1:8095/url?token=密钥    返回 {"url","title"} JSON
       GET http://127.0.0.1:8095/send?group=群号&token=密钥  主动往指定群发图
  3) 命令行:
       python3 daily_image.py                 常驻(QQ@ + HTTP API)
       python3 daily_image.py --url           打印今日图片 URL
       python3 daily_image.py --send 群号      直接向某群发一张

图源: 默认必应每日壁纸(每日更新, 带文字介绍); 失败自动降级到随机美图 API。
纯标准库实现, 不需要 playwright。
"""

import os
import sys
import json
import time
import fcntl
import socket
import base64
import struct
import hashlib
import logging
import argparse
import threading
import urllib.parse
import urllib.request
import resource
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============ 内存限制 ============
MEM_LIMIT_MB = 64
try:
    resource.setrlimit(resource.RLIMIT_AS,
                      (MEM_LIMIT_MB * 1024 * 1024, MEM_LIMIT_MB * 1024 * 1024))
except Exception:
    pass

# ==================== 配置区(都可改) ====================
# 目录结构(脚本在 scripts/, 日志在 logs/):
BASE_DIR = os.path.dirname(os.path.abspath(__file__))    # scripts/ 目录
ROOT_DIR = os.path.dirname(BASE_DIR)                     # 项目根 NapCatQQ-dokcer/
LOG_DIR  = os.path.join(ROOT_DIR, "logs")
LOCK_FILE = os.path.join(BASE_DIR, ".daily_image.lock")  # 进程锁(放 scripts/ 即可)
LOG_FILE  = os.path.join(LOG_DIR, "daily_image.log")

# ---- NapCat ----
WS_HOST, WS_PORT, WS_TOKEN = "127.0.0.1", 3001, "napcat"
HTTP_BASE, HTTP_TOKEN      = "http://127.0.0.1:3000", "napcat"
BOT_QQ      = 1000000001
ALLOW_GROUPS = []                # 空=所有群都能用

# ---- @触发词(想换说法改这里) ----
KW_IMAGE = ["每日一图", "每日图片", "今日美图", "来张图", "每日壁纸"]

# ---- 图源 ----
BING_API = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN"
BING_BASE = "https://www.bing.com"
# 必应失败时的兜底图源(均为直接 302 到图片的接口, NapCat 会自动跟随)
FALLBACK_IMAGES = [
    "https://www.dmoe.cc/random.php",
    "https://img.xjh.me/randomimg.php",
    "https://acg.xydwz.cn/api/api.php",
]

# ---- 防刷: 同群两次@最小间隔(秒) ----
MIN_INTERVAL = 15

# ---- 本机 HTTP API ----
API_HOST = "127.0.0.1"
API_PORT = 8095
API_TOKEN = "napcat-daily"        # ⚠️ 部署后建议改掉; HTTP 调用需带 ?token=
# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("daily_image")

# 今日缓存: {"date": "YYYY-MM-DD", "url": ..., "title": ...}
_CACHE = {"date": None}
_CACHE_LOCK = threading.Lock()
_GROUP_LAST = {}   # gid -> 上次发送 unix 时间


# ---------------- 图源 ----------------
def _http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_today_image(force=False):
    """返回 {'url','title','source'}; 同一天内缓存。必应失败用兜底图源。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _CACHE_LOCK:
        if not force and _CACHE["date"] == today and _CACHE.get("url"):
            return dict(_CACHE)

    url, title, source = None, None, None
    try:
        data = _http_get_json(BING_API)
        img = (data.get("images") or [{}])[0]
        urlbase = img.get("urlbase") or ""
        if urlbase:
            url = f"{BING_BASE}{urlbase}_UHD.jpg"
            title = (img.get("copyright") or "必应每日一图").strip()
            source = "bing"
    except Exception as e:
        log.info(f"必应图源失败: {e}")

    if not url:
        # 兜底: 随机图源(按日期选一个, 当天固定)
        idx = datetime.now().timetuple().tm_yday % len(FALLBACK_IMAGES)
        url = FALLBACK_IMAGES[idx]
        title = "今日随机美图"
        source = "fallback"

    with _CACHE_LOCK:
        _CACHE.update({"date": today, "url": url, "title": title,
                       "source": source})
    return {"url": url, "title": title, "source": source}


# ---------------- NapCat ----------------
def api(action, payload=None):
    req = urllib.request.Request(
        f"{HTTP_BASE}/{action}",
        data=json.dumps(payload or {}).encode(),
        headers={"Authorization": f"Bearer {HTTP_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def send_group_image(group_id, image_url, title=""):
    """发图片 + 文字说明(array 消息)。"""
    msg = [
        {"type": "image", "data": {"file": image_url, "cache": "1"}},
        {"type": "text", "data": {"text": f"\n🖼 每日一图\n{title}"}},
    ]
    return api("send_group_msg", {"group_id": int(group_id), "message": msg})


def send_one(group_id):
    """取今日图并发到指定群, 返回结果 dict。"""
    info = fetch_today_image()
    resp = send_group_image(group_id, info["url"], info["title"])
    ok = isinstance(resp, dict) and resp.get("retcode") == 0
    log.info(f"群{group_id} 发送每日一图 ok={ok} src={info['source']} url={info['url'][:100]}")
    return {"ok": ok, "resp": resp, **info}


# ---------------- WS 客户端 ----------------
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


def on_event(ev):
    if ev.get("post_type") != "message" or ev.get("message_type") != "group":
        return
    group_id = ev.get("group_id")
    if ALLOW_GROUPS and group_id not in ALLOW_GROUPS:
        return
    text, at_self = extract_message(ev.get("message"))
    if not at_self or not any(k in text for k in KW_IMAGE):
        return

    # 同群简单防刷
    now = time.time()
    last = _GROUP_LAST.get(group_id, 0)
    if now - last < MIN_INTERVAL:
        log.info(f"群{group_id} 请求过于频繁, 忽略")
        return
    _GROUP_LAST[group_id] = now

    qq = ev.get("user_id")
    log.info(f"群{group_id} {qq} 请求每日一图")
    try:
        r = send_one(group_id)
        if not r["ok"]:
            # 图片没发出去时, 至少用文本告知
            api("send_group_msg", {"group_id": int(group_id),
                                   "message": f"[CQ:at,qq={qq}] 图片发送失败, 稍后再试"})
    except Exception as e:
        log.info(f"每日一图异常: {e}")


def ws_loop():
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
                pass  # close 失败忽略
            time.sleep(5)


# ---------------- HTTP API ----------------
class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DailyImage/1.0"

    def log_message(self, *args):
        pass  # 安静, 不打访问日志

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._text(200,
                       "每日一图 API\n"
                       "  /url?token=TOKEN            获取今日图片URL(JSON)\n"
                       "  /send?group=群号&token=TOKEN 往QQ群发送今日图片\n")
            return

        token = qs.get("token", [""])[0]
        if token != API_TOKEN:
            self._json(403, {"ok": False, "error": "token 无效(?token=...)"})
            return

        if path == "/url":
            try:
                info = fetch_today_image()
                self._json(200, {"ok": True, **info})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        if path == "/send":
            gid = qs.get("group", [""])[0]
            if not gid.isdigit():
                self._json(400, {"ok": False, "error": "缺少 group=群号"})
                return
            try:
                r = send_one(int(gid))
                self._json(200, {"ok": r["ok"], "title": r["title"],
                                 "url": r["url"], "resp": r["resp"]})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return

        self._json(404, {"ok": False, "error": "未知路径"})


def http_loop():
    try:
        srv = ThreadingHTTPServer((API_HOST, API_PORT), ApiHandler)
    except Exception as e:
        log.info(f"[HTTP API] 启动失败({API_HOST}:{API_PORT}): {e}")
        return
    log.info(f"[HTTP API] 监听 http://{API_HOST}:{API_PORT} (/url /send, token 鉴权)")
    srv.serve_forever()


# ---------------- 主流程 ----------------
def acquire_lock():
    fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp
    except BlockingIOError:
        return None


def main():
    parser = argparse.ArgumentParser(description="每日一图")
    parser.add_argument("--url", action="store_true", help="打印今日图片 URL")
    parser.add_argument("--send", metavar="群号", help="直接向指定群发送")
    args = parser.parse_args()

    if args.url:
        print(json.dumps(fetch_today_image(force=True), ensure_ascii=False, indent=2))
        return
    if args.send:
        r = send_one(int(args.send))
        print("发送成功" if r["ok"] else f"发送失败: {r['resp']}")
        return

    if acquire_lock() is None:
        log.info("已有 daily_image 实例在运行, 退出")
        return
    log.info("=" * 50)
    log.info(f"daily_image 启动 | bot={BOT_QQ} 触发词={KW_IMAGE}")
    threading.Thread(target=http_loop, daemon=True).start()
    ws_loop()


if __name__ == "__main__":
    main()
