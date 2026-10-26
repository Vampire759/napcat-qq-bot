#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
panel_bridge.py — 供 napcat 容器内 web_panel.py 调用的宿主机桥接

网页管理面板跑在 napcat 容器里时, 容器内没有 systemd/journalctl/本地模型,
这些操作全部由本桥接在宿主机上代劳, 面板经 unix socket 发 JSON 请求:

  run/panel_bridge.sock (项目目录已被整体挂载进容器, socket 两边可见)

协议(一行 JSON 请求, 一行 JSON 应答):
  {"action":"services"}                  -> {"ok":true,"services":[{name,active,enabled}]}
  {"action":"svc","name":x,"op":start|stop|restart} -> {"ok":true,"output","active","enabled"}
  {"action":"journal","name":x,"lines":n}-> {"ok":true,"text"}
  {"action":"ai_test","prompt":p}        -> {"ok":true,"success","provider","text"}
  {"action":"restart_ai"}                -> {"ok":true,"output"}

安全: 只接受本机同 UID(或 root)的连接(SO_PEERCRED 校验)。

服务方式: napcat-panel-bridge.service
"""

import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/

import ai_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONF_DIR = os.path.join(ROOT_DIR, "conf")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
RUN_DIR = os.path.join(ROOT_DIR, "run")
SOCK = os.path.join(RUN_DIR, "panel_bridge.sock")
UNIT_DIR = os.path.join(ROOT_DIR, "systemd")
AI_CONFIG = os.path.join(CONF_DIR, "ai_config.json")
LOG_FILE = os.path.join(LOG_DIR, "panel_bridge.log")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bridge] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("panel_bridge")

MAX_REQ = 64 * 1024


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except FileNotFoundError:
        return 127, "命令不存在: " + cmd[0]
    except Exception as e:
        return 1, str(e)


def known_units():
    names = set()
    if os.path.isdir(UNIT_DIR):
        for f in os.listdir(UNIT_DIR):
            if f.endswith(".service"):
                names.add(f[:-len(".service")])
    names.update({"llama-ai", "fnchat-ai", "napcat-listener",
                  "napcat-sign", "qr-forward", "napcat-web-panel"})
    return sorted(names)


def unit_state(name):
    rc_a, active = run(["systemctl", "--user", "is-active", name], 6)
    rc_e, enabled = run(["systemctl", "--user", "is-enabled", name], 6)
    return {"name": name,
            "active": active.splitlines()[0] if active else "unknown",
            "enabled": enabled.splitlines()[0] if enabled else "unknown"}


def load_ai_config():
    try:
        with open(AI_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def action_services(req=None):
    return {"ok": True,
            "services": [unit_state(n) for n in known_units()]}


def action_svc(req):
    name = str(req.get("name") or "")
    op = str(req.get("op") or "")
    if name not in known_units():
        return {"ok": False, "error": "未知服务"}
    if op not in ("start", "stop", "restart"):
        return {"ok": False, "error": "未知操作"}
    log.info("svc %s %s", op, name)
    rc, out = run(["systemctl", "--user", op, name], 40)
    st = unit_state(name)
    if rc != 0:
        return {"ok": False, "error": out or "操作失败", "output": out, **st}
    return {"ok": True, "output": out, **st}


LOGFILE_BY_UNIT = {
    "napcat-listener": "listener.log",
    "napcat-sign": "sign.log",
    "llama-ai": "llama-ai.log",
    "fnchat-ai": "fnchat_ai.log",
    "qr-forward": "qr_forward.log",
    "napcat-daily-image": "daily_image.log",
    "napcat-douyin-spark": "douyin_spark.log",
    "napcat-spark-status": "spark_status.log",
    "napcat-pycreen-present": "pycreen_present.log",
    "napcat-web-panel": "web_panel.log",
    "napcat-panel-bridge": "panel_bridge.log",
}


def _tail(path, lines):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-lines:])
    except Exception:
        return ""


def action_journal(req):
    name = str(req.get("name") or "")
    lines = min(max(int(req.get("lines") or 150), 10), 2000)
    if name not in known_units():
        return {"ok": False, "error": "未知服务"}
    rc, out = run(["journalctl", "--user", "-u", name, "-n", str(lines),
                   "--no-pager", "-o", "cat"], 20)
    text = (out or "").strip()
    # 用户 journal 权限不足/不可用时, 回退读项目自带的服务日志文件
    if len(text) < 20 or "insufficient permissions" in text \
            or "No journal files" in text:
        fname = LOGFILE_BY_UNIT.get(name)
        if fname:
            text = _tail(os.path.join(LOG_DIR, fname), lines)
    return {"ok": True, "text": text or "(无日志)"}


def action_ai_test(req):
    prompt = str(req.get("prompt") or "").strip() or "你好，请用一句话介绍你自己。"
    cfg = load_ai_config()
    log.info("ai_test: %s", prompt[:40])
    ok, provider, text = ai_client.test(cfg, prompt=prompt, log=log.info)
    return {"ok": True, "success": ok, "provider": provider,
            "text": text or "(空)"}


def action_restart_ai(req=None):
    out = []
    ok_all = True
    for u in ("napcat-listener", "fnchat-ai"):
        log.info("restart %s", u)
        rc, o = run(["systemctl", "--user", "restart", u], 40)
        if rc != 0:
            ok_all = False
        out.append(f"{u}: {'ok' if rc == 0 else o or 'failed'}")
    return {"ok": ok_all, "output": "\n".join(out)}


HANDLERS = {
    "services": action_services,
    "svc": action_svc,
    "journal": action_journal,
    "ai_test": action_ai_test,
    "restart_ai": action_restart_ai,
}


def peer_uid(conn):
    """SO_PEERCRED 取对端 UID; 失败返回 None。"""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return uid
    except Exception:
        return None


def handle(conn):
    try:
        allowed = {os.getuid(), 0}
        if peer_uid(conn) not in allowed:
            conn.sendall(b'{"ok":false,"error":"forbidden"}\n')
            return
        f = conn.makefile("rb")
        line = f.readline(MAX_REQ)
        f.close()
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            conn.sendall(b'{"ok":false,"error":"bad request"}\n')
            return
        action = str(req.get("action") or "")
        handler = HANDLERS.get(action)
        if not handler:
            conn.sendall(b'{"ok":false,"error":"unknown action"}\n')
            return
        resp = handler(req)
        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception as e:
        log.info("处理失败: %s", e)
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(e)},
                                     ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        os.chmod(RUN_DIR, 0o750)
    except Exception:
        pass
    if os.path.exists(SOCK):
        # 残留 socket(上次没正常退出)先删掉
        try:
            os.remove(SOCK)
        except Exception:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    try:
        os.chmod(SOCK, 0o660)
    except Exception:
        pass
    srv.listen(4)
    log.info("桥接已启动: %s (uid=%s)", SOCK, os.getuid())
    while True:
        try:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.info("accept 失败: %s", e)
    srv.close()
    try:
        os.remove(SOCK)
    except Exception:
        pass


if __name__ == "__main__":
    main()