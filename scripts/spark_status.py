#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spark_status.py — 抖音「火花状态」查询脚本（独立单文件, 纯标准库, 无头常驻）

职责:
  监听 QQ 群消息, 管理员 @机器人 说「查看火花」时, 读取 douyin_spark.py 自动生成的
  匹配资料配置文件 spark_friends.json 和名单 spark_list.json, 汇总每个抖音号的
  匹配情况 / 火花天数 / 上次续火花时间并回复。

权限:
  仅超级管理员 + admins.json 内的管理员可用; 普通成员 @ 一律静默(不暴露功能存在)。

@指令(关键词可在下面配置区改):
  @机器人 查看火花      查看全部状态
  @机器人 火花状态      同上(别名)

命令行:
  python3 spark_status.py             常驻监听
  python3 spark_status.py --print     不连 QQ, 直接把状态打印到终端

说明: 本脚本只读不写, 不依赖 playwright; 匹配资料由 douyin_spark.py 生成与更新。
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
import urllib.request
import resource
from datetime import datetime

# ============ 内存限制 ============
MEM_LIMIT_MB = 64
try:
    resource.setrlimit(resource.RLIMIT_AS,
                      (MEM_LIMIT_MB * 1024 * 1024, MEM_LIMIT_MB * 1024 * 1024))
except Exception:
    pass

# ==================== 配置区 ====================
# 目录结构(脚本在 scripts/, 配置/状态在 conf/, 日志在 logs/):
BASE_DIR = os.path.dirname(os.path.abspath(__file__))    # scripts/ 目录
ROOT_DIR = os.path.dirname(BASE_DIR)                     # 项目根 NapCatQQ-dokcer/
CONF_DIR = os.path.join(ROOT_DIR, "conf")
LOG_DIR  = os.path.join(ROOT_DIR, "logs")
LIST_FILE    = os.path.join(CONF_DIR, "spark_list.json")      # 名单(douyin_spark.py 维护)
FRIENDS_FILE = os.path.join(CONF_DIR, "spark_friends.json")   # 匹配资料(douyin_spark.py 生成)
STATE_FILE   = os.path.join(CONF_DIR, "douyin_state.json")    # 登录态(只判断存在性)
ADMIN_FILE   = os.path.join(CONF_DIR, "admins.json")          # 与 listener.py 共用
LOCK_FILE    = os.path.join(BASE_DIR, ".spark_status.lock")   # 进程锁(放 scripts/ 即可)
LOG_FILE     = os.path.join(LOG_DIR, "spark_status.log")

WS_HOST, WS_PORT, WS_TOKEN = "127.0.0.1", 3001, "napcat"
HTTP_BASE, HTTP_TOKEN      = "http://127.0.0.1:3000", "napcat"
BOT_QQ      = 1000000001
SUPER_ADMIN = 1000000002
ALLOW_GROUPS = []                 # 空=所有群

# @触发关键词(可改)
KW_VIEW = ["查看火花", "火花状态", "续火花状态", "火花情况"]

# 下一轮自动发送时刻(仅用于展示, 需与 douyin_spark.py 配置一致)
FIRE_LABEL = "00:00:01"
# ==============================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("spark_status")


# ---------------- 数据读取 ----------------
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.info(f"读取 {os.path.basename(path)} 失败: {e}")
        return default


def load_admins():
    try:
        return {int(x) for x in _load_json(ADMIN_FILE, {}).get("admins", [])}
    except Exception:
        return set()


def is_privileged(qq):
    return int(qq) == int(SUPER_ADMIN) or int(qq) in load_admins()


def _short_time(s):
    """'2026-09-16 18:09:56' -> '18:09'(今天) / '09-15 22:10'(非今天)。
    空/从未 返回空串。解析失败原样返回前10位。"""
    if not s or s == "从未":
        return ""
    try:
        dt = datetime.fromisoformat(s)
        if dt.date() == datetime.now().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(s)[:10]


def build_status():
    """汇总名单 + 匹配资料, 返回精简回复: 每个好友一行, 一屏能看全。"""
    targets = _load_json(LIST_FILE, {}).get("targets", [])
    store = _load_json(FRIENDS_FILE, {})
    friends = store.get("friends", {})

    total = len(targets)
    matched = sum(1 for t in targets
                  if friends.get(str(t.get("douyin_id")), {}).get("matched"))
    # 头部: 匹配数 + 登录账号(没拿到就只看登录态文件)
    who = store.get("my_nickname") or store.get("my_douyin_id")
    if who:
        head = f"🔥 续火花 {matched}/{total} · 登录:{who}"
    elif os.path.exists(STATE_FILE):
        head = f"🔥 续火花 {matched}/{total} · 登录:✅"
    else:
        head = f"🔥 续火花 {matched}/{total} · ⚠️未登录(先跑 douyin_qrlogin)"
    lines = [head]

    if not targets:
        lines.append("名单为空, @我 添加续火花 抖音号")
    for t in targets:
        dy = str(t.get("douyin_id"))
        info = friends.get(dy, {})
        nick = info.get("nickname") or t.get("nickname") or dy
        if info.get("matched"):
            # 已匹配: 🟢 昵称 天数 上次续火时间+结果, 一行完事
            seg = f"🟢 {nick}"
            days = info.get("spark_days")
            if days is not None:
                seg += f" {days}天"
            last = _short_time(info.get("last_send"))
            if last:
                seg += f" {last}{'✅' if info.get('last_status') == 'ok' else '⚠️'}"
            lines.append(seg)
        else:
            # 未匹配: 🔴 昵称 简短原因
            err = info.get("last_error") or "未匹配"
            lines.append(f"🔴 {nick} {err}")

    # 下轮时间: 今天 00:00:01 已过则展示明天(逻辑与旧版一致)
    now = datetime.now()
    candidate = now.replace(hour=0, minute=0, second=1, microsecond=0)
    if candidate <= now:
        candidate = datetime.fromtimestamp(time.mktime(
            time.localtime(time.time() + 86400)[:3] + (0, 0, 1, 0, 0, -1)))
    lines.append(f"下轮 {candidate.strftime('%m-%d')} {FIRE_LABEL}")
    return "\n".join(lines)


# ---------------- NapCat HTTP / WS ----------------
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
    qq = ev.get("user_id")
    text, at_self = extract_message(ev.get("message"))
    if not at_self or not any(k in text for k in KW_VIEW):
        return
    if not is_privileged(qq):
        # 普通成员无权调用: 静默
        log.info(f"群{group_id} 普通成员 {qq} 尝试查看火花, 已忽略")
        return
    time.sleep(1)
    send_group(group_id, f"[CQ:at,qq={qq}]\n{build_status()}")
    log.info(f"群{group_id} 管理员 {qq} 查看火花, 已回复")


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
    parser = argparse.ArgumentParser(description="抖音火花状态查询")
    parser.add_argument("--print", dest="do_print", action="store_true",
                        help="直接打印状态, 不连 QQ")
    args = parser.parse_args()
    if args.do_print:
        print(build_status())
        return

    if acquire_lock() is None:
        log.info("已有 spark_status 实例在运行, 退出")
        return
    log.info("=" * 50)
    log.info(f"spark_status 启动 | bot={BOT_QQ} 触发词={KW_VIEW} (仅管理员)")
    while True:
        ws = WSClient(WS_HOST, WS_PORT, WS_TOKEN)
        try:
            ws.connect()
            log.info("[WS] 已连接 NapCat")
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


if __name__ == "__main__":
    main()
