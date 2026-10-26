#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_panel.py — NapCatQQ-dokcer 网页管理面板(纯标准库, 无第三方依赖)

打开浏览器访问 http://服务器IP:8090 即可:

  • 服务管理   查看/启动/停止/重启 systemd 用户服务(listener/打卡/火花/AI...)
  • 脚本管理   列出 scripts/ 下的脚本, 在线查看/编辑/新建/删除(自动备份)
  • AI 配置    在线编辑 conf/ai_config.json, 快捷设置外部 API(API 优先, 失败回退本地)
  • AI 测试    直接发一句话, 看这次回答来自「外部 API」还是「本地模型」
  • 日志查看   logs/*.log 与 journalctl 服务日志

登录:
  首次启动自动生成 conf/web_panel.json(含随机访问令牌), 启动时日志里会打印。
  浏览器输入令牌即可登录; 也可用 http://IP:8090/?token=令牌 直接进入。

启动:
  python3 scripts/web_panel.py                   前台运行
  systemctl --user restart napcat-web-panel       服务方式(推荐)

配置(conf/web_panel.json, 不入库):
  {"host": "0.0.0.0", "port": 8090, "token": "随机令牌"}
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ai_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONF_DIR = os.path.join(ROOT_DIR, "conf")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
BACKUP_DIR = os.path.join(ROOT_DIR, "back", "panel")
PANEL_CONF = os.path.join(CONF_DIR, "web_panel.json")
AI_CONFIG = os.path.join(CONF_DIR, "ai_config.json")
AI_EXAMPLE = os.path.join(CONF_DIR, "ai_config.example.json")
UNIT_DIR = os.path.join(ROOT_DIR, "systemd")

MAX_BODY = 2 * 1024 * 1024          # 单次请求体上限 2MB
EDITABLE_TEXT = {                   # 允许在线编辑的 conf 文本文件
    "personality.md": os.path.join(CONF_DIR, "personality.md"),
    "ai_rules.md": os.path.join(CONF_DIR, "ai_rules.md"),
}
SESSIONS = {}                       # cookie 值 -> 过期时间戳


# ---------------------------------------------------------------- 配置
def load_panel_config():
    """读取 conf/web_panel.json; 不存在则生成随机令牌并写盘。"""
    cfg = {"host": "0.0.0.0", "port": 8090, "token": ""}
    try:
        with open(PANEL_CONF, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(12)
        save_json(PANEL_CONF, cfg, mode=0o600)
        print(f"[面板] 已生成访问令牌并写入 {PANEL_CONF}", flush=True)
    if not os.path.exists(PANEL_CONF):
        save_json(PANEL_CONF, cfg, mode=0o600)
    return cfg


def save_json(path, obj, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    if mode:
        try:
            os.chmod(path, mode)
        except Exception:
            pass


def load_ai_config():
    cfg = {}
    try:
        with open(AI_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        pass
    if not cfg:
        try:
            with open(AI_EXAMPLE, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            cfg = {}
    return cfg


# ---------------------------------------------------------------- 工具
def run(cmd, timeout=15):
    """执行命令, 返回 (返回码, 输出文本)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except FileNotFoundError:
        return 127, "命令不存在: " + cmd[0]
    except Exception as e:
        return 1, str(e)


def known_units():
    """服务列表来源: systemd/ 目录下的 unit 文件 + 手册里提到但可能缺文件的。"""
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
    return {
        "name": name,
        "active": active.splitlines()[0] if active else "unknown",
        "enabled": enabled.splitlines()[0] if enabled else "unknown",
    }


def safe_script_path(name):
    """只允许 scripts/ 下的普通文件名, 防目录穿越。"""
    base = os.path.basename(name or "")
    if not base or base != name:
        raise ValueError("非法文件名")
    if not base.endswith((".py", ".sh")):
        raise ValueError("只允许 .py / .sh")
    path = os.path.join(SCRIPTS_DIR, base)
    if os.path.commonpath([os.path.abspath(path), SCRIPTS_DIR]) != SCRIPTS_DIR:
        raise ValueError("非法路径")
    return path


def safe_log_path(name):
    base = os.path.basename(name or "")
    if not base or base != name or not base.endswith(".log"):
        raise ValueError("非法日志名")
    path = os.path.join(LOG_DIR, base)
    if not os.path.exists(path):
        raise ValueError("日志不存在")
    return path


def tail(path, lines=200, max_bytes=256 * 1024):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-max(0, int(lines)):])


def backup(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(path, dst)
        return dst
    except Exception:
        return ""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "NapCatPanel/1.0"
    cfg = {"token": ""}

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        print(f"[面板] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    # ---------- 鉴权 ----------
    def _authed(self):
        token = self.cfg.get("token") or ""
        if not token:
            return False
        if self.headers.get("X-Token") == token:
            return True
        now = time.time()
        for k, exp in list(SESSIONS.items()):
            if exp < now:
                SESSIONS.pop(k, None)
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "panel_token" and v in SESSIONS:
                    return True
        return False

    def _need_auth(self):
        if self._authed():
            return True
        self._err("未登录或令牌已失效", 401)
        return False

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            token = (qs.get("token") or [None])[0]
            extra = {}
            if token and token == self.cfg.get("token"):
                sid, exp = self._new_session()
                extra["Set-Cookie"] = (f"panel_token={sid}; Path=/; HttpOnly; "
                                       f"SameSite=Strict; Max-Age=2592000")
            return self._send(200, PAGE, "text/html; charset=utf-8", extra)
        if not url.path.startswith("/api/"):
            return self._err("不存在", 404)
        if not self._need_auth():
            return
        try:
            self._get_api(url.path, qs)
        except ValueError as e:
            self._err(e)
        except Exception as e:
            self._err(f"服务器错误: {e}", 500)

    def _get_api(self, path, qs):
        if path == "/api/overview":
            ai_cfg = load_ai_config()
            services = [unit_state(n) for n in known_units()]
            return self._json({
                "ok": True,
                "services": services,
                "ai": ai_client.api_info(ai_cfg),
                "ai_enabled": bool(ai_cfg.get("enabled", True)),
                "local_url": ai_cfg.get("api_url", ""),
                "local_model": ai_cfg.get("model", ""),
                "root": ROOT_DIR,
                "units": known_units(),
            })
        if path == "/api/service/state":
            name = (qs.get("name") or [""])[0]
            if name not in known_units():
                raise ValueError("未知服务")
            return self._json({"ok": True, **unit_state(name)})
        if path == "/api/service/logs":
            name = (qs.get("name") or [""])[0]
            lines = int((qs.get("lines") or ["150"])[0])
            if name not in known_units():
                raise ValueError("未知服务")
            rc, out = run(["journalctl", "--user", "-u", name, "-n", str(min(lines, 2000)),
                           "--no-pager", "-o", "cat"], 20)
            return self._json({"ok": rc in (0, 1), "text": out or "(无日志)"})
        if path == "/api/scripts":
            items = []
            if os.path.isdir(SCRIPTS_DIR):
                for f in sorted(os.listdir(SCRIPTS_DIR)):
                    p = os.path.join(SCRIPTS_DIR, f)
                    if not os.path.isfile(p) or not f.endswith((".py", ".sh")):
                        continue
                    st = os.stat(p)
                    items.append({"name": f, "size": st.st_size,
                                  "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                         time.localtime(st.st_mtime)),
                                  "exec": bool(st.st_mode & 0o111)})
            return self._json({"ok": True, "items": items})
        if path == "/api/script":
            name = (qs.get("name") or [""])[0]
            p = safe_script_path(name)
            if not os.path.exists(p):
                raise ValueError("脚本不存在")
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return self._json({"ok": True, "name": name, "content": f.read()})
        if path == "/api/config":
            with open(AI_CONFIG, "r", encoding="utf-8") as f:
                raw = f.read()
            return self._json({"ok": True, "raw": raw, "parsed": load_ai_config(),
                               "api": ai_client.api_info(load_ai_config())})
        if path == "/api/textfile":
            name = (qs.get("name") or [""])[0]
            if name not in EDITABLE_TEXT:
                raise ValueError("不允许的文件")
            p = EDITABLE_TEXT[name]
            if not os.path.exists(p):
                return self._json({"ok": True, "name": name, "content": ""})
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return self._json({"ok": True, "name": name, "content": f.read()})
        if path == "/api/logs":
            items = []
            if os.path.isdir(LOG_DIR):
                for f in sorted(os.listdir(LOG_DIR)):
                    p = os.path.join(LOG_DIR, f)
                    if os.path.isfile(p) and f.endswith(".log"):
                        items.append({"name": f,
                                      "size": os.path.getsize(p)})
            return self._json({"ok": True, "items": items})
        if path == "/api/log":
            name = (qs.get("name") or [""])[0]
            lines = int((qs.get("lines") or ["200"])[0])
            p = safe_log_path(name)
            return self._json({"ok": True, "name": name,
                               "text": tail(p, min(max(lines, 10), 3000))})
        raise ValueError("未知接口")

    # ---------- POST ----------
    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self._body()
        except ValueError as e:
            return self._err(e)
        if url.path == "/api/login":
            token = str(body.get("token") or "")
            if not hmac.compare_digest(token, self.cfg.get("token") or ""):
                time.sleep(0.8)
                return self._err("令牌错误", 401)
            sid, exp = self._new_session()
            return self._send(
                200, json.dumps({"ok": True, "token": self.cfg["token"]}),
                extra={"Set-Cookie": (f"panel_token={sid}; Path=/; HttpOnly; "
                                      f"SameSite=Strict; Max-Age=2592000")})
        if url.path == "/api/logout":
            SESSIONS.clear()
            return self._send(200, json.dumps({"ok": True}),
                              extra={"Set-Cookie": "panel_token=; Path=/; Max-Age=0"})
        if not self._need_auth():
            return
        try:
            self._post_api(url.path, body)
        except ValueError as e:
            self._err(e)
        except Exception as e:
            self._err(f"服务器错误: {e}", 500)

    def _new_session(self):
        sid = secrets.token_urlsafe(24)
        exp = time.time() + 30 * 86400
        SESSIONS[sid] = exp
        return sid, exp

    def _post_api(self, path, body):
        if path == "/api/service":
            name = str(body.get("name") or "")
            action = str(body.get("action") or "")
            if name not in known_units():
                raise ValueError("未知服务")
            if action not in ("start", "stop", "restart"):
                raise ValueError("未知操作")
            rc, out = run(["systemctl", "--user", action, name], 40)
            return self._json({"ok": rc == 0, "output": out, **unit_state(name)})
        if path == "/api/script/save":
            name = str(body.get("name") or "")
            content = body.get("content")
            if not isinstance(content, str):
                raise ValueError("缺少内容")
            p = safe_script_path(name)
            bak = backup(p) if os.path.exists(p) else ""
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            run(["chmod", "+x", p], 5)
            return self._json({"ok": True, "backup": bak})
        if path == "/api/script/create":
            name = str(body.get("name") or "")
            p = safe_script_path(name)
            if os.path.exists(p):
                raise ValueError("文件已存在")
            with open(p, "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n")
            run(["chmod", "+x", p], 5)
            return self._json({"ok": True})
        if path == "/api/script/delete":
            name = str(body.get("name") or "")
            p = safe_script_path(name)
            if not os.path.exists(p):
                raise ValueError("脚本不存在")
            backup(p)
            os.remove(p)
            return self._json({"ok": True})
        if path == "/api/config":
            cfg = body.get("config")
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except Exception as e:
                    raise ValueError(f"JSON 语法错误: {e}")
            if not isinstance(cfg, dict):
                raise ValueError("配置必须是 JSON 对象")
            backup(AI_CONFIG)
            save_json(AI_CONFIG, cfg)
            return self._json({"ok": True, "api": ai_client.api_info(cfg)})
        if path == "/api/config/patch":
            patch = body.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("patch 必须是对象")
            cfg = load_ai_config()
            cfg.update(patch)
            backup(AI_CONFIG)
            save_json(AI_CONFIG, cfg)
            return self._json({"ok": True, "config": cfg,
                               "api": ai_client.api_info(cfg)})
        if path == "/api/textfile":
            name = str(body.get("name") or "")
            content = body.get("content")
            if name not in EDITABLE_TEXT:
                raise ValueError("不允许的文件")
            if not isinstance(content, str):
                raise ValueError("缺少内容")
            p = EDITABLE_TEXT[name]
            bak = backup(p) if os.path.exists(p) else ""
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            return self._json({"ok": True, "backup": bak})
        if path == "/api/ai/test":
            prompt = str(body.get("prompt") or "").strip() or "你好，请用一句话介绍你自己。"
            cfg = load_ai_config()
            ok, provider, text = ai_client.test(cfg, prompt=prompt)
            return self._json({"ok": ok, "provider": provider, "text": text,
                               "api": ai_client.api_info(cfg)})
        if path == "/api/ai/reload":
            # 重启 AI 相关服务, 让新配置生效(配置文件只在启动时读取)
            out = []
            for u in ("napcat-listener", "fnchat-ai"):
                rc, o = run(["systemctl", "--user", "restart", u], 40)
                out.append(f"{u}: {'ok' if rc == 0 else o or 'failed'}")
            return self._json({"ok": True, "output": "\n".join(out)})
        raise ValueError("未知接口")


# ---------------------------------------------------------------- 页面
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NapCat 管理面板</title>
<style>
*{box-sizing:border-box}
:root{
  --bg:#0f1420; --panel:#161c2b; --panel2:#1d2536; --line:#2a3348;
  --txt:#e6e9f2; --dim:#8b96ad; --accent:#4f8cff; --ok:#2fbf71;
  --warn:#e6a23c; --bad:#e55353;
}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--txt)}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;
  background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;font-weight:600}
header .sp{flex:1}
.badge{font-size:12px;padding:2px 8px;border-radius:10px;background:var(--panel2);
  color:var(--dim);border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:#1e5c3a}
.badge.bad{color:var(--bad);border-color:#5c2222}
main{padding:18px 20px;max-width:1180px;margin:0 auto}
nav{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
nav button{background:var(--panel2);color:var(--dim);border:1px solid var(--line);
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px}
nav button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px;margin-bottom:16px}
.card h3{margin:0 0 12px;font-size:14px;font-weight:600;color:var(--txt)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500}
td.mono,pre,textarea,.mono{font-family:ui-monospace,Consolas,"Courier New",monospace}
button.act{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
  padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;margin-right:4px}
button.act:hover{border-color:var(--accent);color:#fff}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);
  padding:7px 16px;border-radius:8px;cursor:pointer;font-size:13px}
button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line);
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px}
input,textarea,select{background:#0b1019;color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;font-size:13px;width:100%}
textarea{resize:vertical;line-height:1.5}
label.f{display:block;color:var(--dim);font-size:12px;margin:10px 0 4px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>.col{flex:1;min-width:220px}
pre{background:#0b1019;border:1px solid var(--line);border-radius:8px;padding:12px;
  overflow:auto;max-height:480px;white-space:pre-wrap;word-break:break-all;font-size:12px}
.hint{color:var(--dim);font-size:12px;margin-top:6px}
#login{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;
  justify-content:center;z-index:50}
#login .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:26px;width:340px}
#login h2{margin:0 0 6px;font-size:16px}
#login p{color:var(--dim);font-size:12px;margin:0 0 14px}
.toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);
  border:1px solid var(--line);padding:10px 14px;border-radius:8px;font-size:13px;
  opacity:0;transition:.25s;z-index:80;max-width:60vw}
.toast.show{opacity:1}
.hide{display:none!important}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
</style>
</head>
<body>
<div id="login">
  <div class="box">
    <h2>NapCat 管理面板</h2>
    <p>请输入访问令牌(见 conf/web_panel.json 或服务启动日志)</p>
    <input id="tok" type="password" placeholder="访问令牌" autocomplete="current-password">
    <div style="height:12px"></div>
    <button class="primary" style="width:100%" onclick="login()">登录</button>
    <p id="lerr" class="bad" style="margin-top:10px"></p>
  </div>
</div>

<header>
  <h1>NapCat 管理面板</h1>
  <span id="aiBadge" class="badge">AI …</span>
  <span class="sp"></span>
  <button class="ghost" onclick="logout()">退出</button>
</header>

<main>
  <nav>
    <button class="on" data-tab="svc">服务</button>
    <button data-tab="ai">AI 配置</button>
    <button data-tab="script">脚本</button>
    <button data-tab="log">日志</button>
  </nav>

  <section id="tab-svc">
    <div class="card">
      <h3>systemd 用户服务</h3>
      <table><thead><tr><th>服务</th><th>状态</th><th>开机自启</th><th>操作</th></tr></thead>
      <tbody id="svcBody"><tr><td colspan="4" class="hint">加载中…</td></tr></tbody></table>
      <div class="hint">AI 单独重启：<button class="act" onclick="restartAI()">重启 listener + fnchat-ai</button></div>
    </div>
    <div class="card">
      <h3>服务日志（journalctl）</h3>
      <div class="row">
        <div class="col"><select id="svcLogSel"></select></div>
        <div class="col" style="max-width:150px"><button class="act" onclick="loadSvcLog()">刷新</button></div>
      </div>
      <pre id="svcLog">选择服务后点击刷新</pre>
    </div>
  </section>

  <section id="tab-ai" class="hide">
    <div class="card">
      <h3>外部 API（优先调用，失败自动回退本地）</h3>
      <div class="row">
        <div class="col">
          <label class="f">总开关</label>
          <select id="api_enabled"><option value="true">开启（API 优先）</option><option value="false">关闭（只用本地）</option></select>
        </div>
        <div class="col">
          <label class="f">API 根地址（OpenAI 兼容，带不带 /v1 都行）</label>
          <input id="api_base_url" placeholder="https://api.deepseek.com">
        </div>
      </div>
      <div class="row">
        <div class="col">
          <label class="f">API Key</label>
          <input id="api_key" type="password" placeholder="sk-…（留空=沿用已保存）">
        </div>
        <div class="col">
          <label class="f">API 模型名</label>
          <input id="api_model" placeholder="deepseek-chat">
        </div>
      </div>
      <div class="row">
        <div class="col">
          <label class="f">API 超时（秒）</label>
          <input id="api_timeout" type="number" min="5" max="600">
        </div>
        <div class="col">
          <label class="f">失败回退本地</label>
          <select id="api_fallback_local"><option value="true">是</option><option value="false">否（失败直接不回复）</option></select>
        </div>
        <div class="col">
          <label class="f">代理（可选）</label>
          <input id="api_proxy" placeholder="http://127.0.0.1:7890">
        </div>
      </div>
      <div style="margin-top:14px">
        <button class="primary" onclick="saveApi()">保存 API 设置</button>
        <button class="ghost" onclick="loadConfig()">重新读取</button>
      </div>
      <div class="hint" id="apiState"></div>
    </div>

    <div class="card">
      <h3>AI 测试</h3>
      <div class="row">
        <div class="col"><input id="testPrompt" placeholder="你好，请用一句话介绍你自己。"></div>
        <div style="max-width:130px"><button class="primary" style="width:100%" onclick="testAI()">发送测试</button></div>
      </div>
      <div class="hint" id="testMeta"></div>
      <pre id="testOut">尚未测试</pre>
    </div>

    <div class="card">
      <h3>完整配置 conf/ai_config.json</h3>
      <textarea id="cfgRaw" rows="18" spellcheck="false"></textarea>
      <div class="hint">改完点保存即可写入文件；服务需要重启才读到新配置（可用上方「重启 listener + fnchat-ai」）。</div>
      <div style="margin-top:10px">
        <button class="primary" onclick="saveConfig()">保存完整配置</button>
      </div>
    </div>

    <div class="card">
      <h3>人格 / 规则（热加载，保存即生效）</h3>
      <div class="row">
        <div class="col"><label class="f">人格 conf/personality.md</label>
          <textarea id="persona" rows="9" spellcheck="false"></textarea></div>
        <div class="col"><label class="f">规则 conf/ai_rules.md</label>
          <textarea id="rules" rows="9" spellcheck="false"></textarea></div>
      </div>
      <div style="margin-top:10px">
        <button class="primary" onclick="saveText('personality.md','persona')">保存人格</button>
        <button class="primary" onclick="saveText('ai_rules.md','rules')">保存规则</button>
      </div>
    </div>
  </section>

  <section id="tab-script" class="hide">
    <div class="card">
      <h3>脚本列表（scripts/）</h3>
      <table><thead><tr><th>文件名</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead>
      <tbody id="scrBody"><tr><td colspan="4" class="hint">加载中…</td></tr></tbody></table>
      <div class="row" style="margin-top:12px">
        <div class="col"><input id="newScript" placeholder="新建脚本名，如 my_task.py"></div>
        <div style="max-width:120px"><button class="act" onclick="createScript()">新建</button></div>
      </div>
    </div>
    <div class="card">
      <h3 id="scrTitle">编辑脚本</h3>
      <textarea id="scrEdit" rows="24" spellcheck="false" placeholder="从上方选择脚本"></textarea>
      <div style="margin-top:10px">
        <button class="primary" onclick="saveScript()">保存（自动备份）</button>
        <span class="hint" id="scrHint"></span>
      </div>
    </div>
  </section>

  <section id="tab-log" class="hide">
    <div class="card">
      <h3>日志文件（logs/）</h3>
      <div class="row">
        <div class="col"><select id="logSel"></select></div>
        <div style="max-width:130px"><input id="logLines" type="number" value="200" min="20" max="3000"></div>
        <div style="max-width:120px"><button class="act" onclick="loadLog()">刷新</button></div>
      </div>
      <pre id="logOut">选择日志后点击刷新</pre>
    </div>
  </section>
</main>

<div id="toast" class="toast"></div>

<script>
let TOKEN = localStorage.getItem('panel_token') || '';
let SCR = '';
const $ = id => document.getElementById(id);

function toast(msg, bad){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(t._h);
  t._h = setTimeout(()=>t.className='toast', 2600);
}
function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({'Content-Type':'application/json','X-Token':TOKEN}, opts.headers||{});
  return fetch(path, opts).then(async r => {
    let data = {};
    try { data = await r.json(); } catch(e){}
    if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('未登录'); }
    if (!r.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  });
}
function showLogin(){ $('login').classList.remove('hide'); }
async function login(){
  const tok = $('tok').value.trim();
  if (!tok) return;
  try{
    const r = await api('/api/login', {method:'POST', body: JSON.stringify({token: tok})});
    TOKEN = r.token; localStorage.setItem('panel_token', TOKEN);
    $('login').classList.add('hide'); $('lerr').textContent='';
    init();
  }catch(e){ $('lerr').textContent = e.message; }
}
function logout(){
  api('/api/logout', {method:'POST', body:'{}'}).catch(()=>{});
  TOKEN=''; localStorage.removeItem('panel_token'); showLogin();
}
document.querySelectorAll('nav button').forEach(b=>{
  b.onclick = () => {
    document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on', x===b));
    ['svc','ai','script','log'].forEach(t=>$('tab-'+t).classList.toggle('hide', t!==b.dataset.tab));
    if (b.dataset.tab==='ai') loadConfig();
    if (b.dataset.tab==='script') loadScripts();
    if (b.dataset.tab==='log') loadLogList();
  };
});

function fmtState(s){
  const cls = s==='active' ? 'ok' : (s==='inactive'||s==='failed' ? 'bad' : 'warn');
  return '<span class="'+cls+'">'+s+'</span>';
}
async function loadSvc(){
  try{
    const d = await api('/api/overview');
    $('svcBody').innerHTML = d.services.map(s=>`
      <tr><td class="mono">${s.name}</td>
      <td>${fmtState(s.active)}</td>
      <td>${s.enabled==='enabled'?'<span class="ok">已启用</span>':s.enabled}</td>
      <td>
        <button class="act" onclick="svc('${s.name}','start')">启动</button>
        <button class="act" onclick="svc('${s.name}','stop')">停止</button>
        <button class="act" onclick="svc('${s.name}','restart')">重启</button>
        <button class="act" onclick="pickSvcLog('${s.name}')">日志</button>
      </td></tr>`).join('');
    const sel = $('svcLogSel');
    if (!sel.options.length) sel.innerHTML = d.units.map(u=>`<option>${u}</option>`).join('');
    const aiInfo = d.ai;
    const badge = $('aiBadge');
    if (aiInfo.ready) { badge.className='badge ok'; badge.textContent='AI: 外部 API 优先'; }
    else { badge.className='badge'; badge.textContent='AI: 本地模型'; }
    $('apiState').innerHTML = aiInfo.ready
      ? '当前：外部 API 已启用 → ' + aiInfo.base_url + (aiInfo.model?(' / '+aiInfo.model):'') + (aiInfo.key_hint?('，Key '+aiInfo.key_hint):'')
      : '当前：外部 API 未配置，使用本地模型 ' + (d.local_url||'');
  }catch(e){ toast(e.message, true); }
}
async function svc(name, action){
  try{ const r = await api('/api/service', {method:'POST', body: JSON.stringify({name, action})});
    toast(name + ' ' + action + ' → ' + r.active); loadSvc();
  }catch(e){ toast(e.message, true); }
}
function pickSvcLog(name){ $('svcLogSel').value = name; loadSvcLog(); }
async function loadSvcLog(){
  const name = $('svcLogSel').value;
  if (!name) return;
  try{ const d = await api('/api/service/logs?name='+encodeURIComponent(name)+'&lines=150');
    $('svcLog').textContent = d.text; }catch(e){ toast(e.message, true); }
}
async function restartAI(){
  try{ const d = await api('/api/ai/reload', {method:'POST', body:'{}'});
    toast('已重启：' + d.output.replace(/\n/g,' | ')); loadSvc();
  }catch(e){ toast(e.message, true); }
}

function fillApi(cfg){
  $('api_enabled').value = String(cfg.api_enabled !== false);
  $('api_base_url').value = cfg.api_base_url || '';
  $('api_key').value = '';
  $('api_key').placeholder = cfg.api_key ? '已保存，留空则不变' : 'sk-…';
  $('api_model').value = cfg.api_model || '';
  $('api_timeout').value = cfg.api_timeout || 60;
  $('api_fallback_local').value = String(cfg.api_fallback_local !== false);
  $('api_proxy').value = cfg.api_proxy || '';
}
async function loadConfig(){
  try{
    const d = await api('/api/config');
    $('cfgRaw').value = JSON.stringify(d.parsed, null, 2);
    fillApi(d.parsed);
    const t = await api('/api/textfile?name=personality.md');
    $('persona').value = t.content;
    const r = await api('/api/textfile?name=ai_rules.md');
    $('rules').value = r.content;
    $('apiState').innerHTML = d.api.ready
      ? '当前：外部 API 已启用 → ' + d.api.base_url + (d.api.model?(' / '+d.api.model):'')
      : '当前：外部 API 未配置，使用本地模型';
  }catch(e){ toast(e.message, true); }
}
async function saveApi(){
  const patch = {
    api_enabled: $('api_enabled').value === 'true',
    api_base_url: $('api_base_url').value.trim(),
    api_model: $('api_model').value.trim(),
    api_timeout: parseInt($('api_timeout').value || '60', 10),
    api_fallback_local: $('api_fallback_local').value === 'true',
    api_proxy: $('api_proxy').value.trim(),
  };
  const key = $('api_key').value.trim();
  if (key) patch.api_key = key;
  try{ const d = await api('/api/config/patch', {method:'POST', body: JSON.stringify({patch})});
    toast('API 设置已保存');
    await loadConfig();
  }catch(e){ toast(e.message, true); }
}
async function saveConfig(){
  let obj;
  try { obj = JSON.parse($('cfgRaw').value); }
  catch(e){ return toast('JSON 语法错误：' + e.message, true); }
  try{ await api('/api/config', {method:'POST', body: JSON.stringify({config: obj})});
    toast('配置已保存，记得重启服务'); loadConfig();
  }catch(e){ toast(e.message, true); }
}
async function saveText(name, id){
  try{ await api('/api/textfile', {method:'POST', body: JSON.stringify({name, content: $(id).value})});
    toast(name + ' 已保存（热加载，无需重启）');
  }catch(e){ toast(e.message, true); }
}
async function testAI(){
  const prompt = $('testPrompt').value.trim();
  $('testOut').textContent = '请求中…（本地模型可能需要几十秒）';
  $('testMeta').textContent = '';
  try{
    const d = await api('/api/ai/test', {method:'POST', body: JSON.stringify({prompt})});
    $('testMeta').innerHTML = d.ok
      ? '本次回答来源：<b class="'+(d.provider==='api'?'ok':'warn')+'">'+(d.provider==='api'?'外部 API':'本地模型')+'</b>'
      : '<span class="bad">调用失败</span>';
    $('testOut').textContent = d.text || '(空)';
  }catch(e){ $('testMeta').textContent=''; $('testOut').textContent = '失败：' + e.message; }
}

async function loadScripts(){
  try{
    const d = await api('/api/scripts');
    $('scrBody').innerHTML = d.items.map(s=>`
      <tr><td class="mono">${s.name}</td><td>${s.size} B</td><td>${s.mtime}</td>
      <td><button class="act" onclick="openScript('${s.name}')">编辑</button>
          <button class="act" onclick="delScript('${s.name}')">删除</button></td></tr>`).join('')
      || '<tr><td colspan="4" class="hint">没有脚本</td></tr>';
  }catch(e){ toast(e.message, true); }
}
async function openScript(name){
  try{ const d = await api('/api/script?name='+encodeURIComponent(name));
    SCR = name; $('scrTitle').textContent = '编辑：' + name;
    $('scrEdit').value = d.content; $('scrHint').textContent = '';
  }catch(e){ toast(e.message, true); }
}
async function saveScript(){
  if (!SCR) return toast('先选择一个脚本', true);
  try{ const d = await api('/api/script/save', {method:'POST', body: JSON.stringify({name: SCR, content: $('scrEdit').value})});
    $('scrHint').textContent = d.backup ? ('已备份到 ' + d.backup) : '';
    toast('已保存 ' + SCR); loadScripts();
  }catch(e){ toast(e.message, true); }
}
async function createScript(){
  const name = $('newScript').value.trim();
  if (!name) return;
  try{ await api('/api/script/create', {method:'POST', body: JSON.stringify({name})});
    $('newScript').value=''; loadScripts(); openScript(name);
  }catch(e){ toast(e.message, true); }
}
async function delScript(name){
  if (!confirm('删除 ' + name + ' ？（会先备份）')) return;
  try{ await api('/api/script/delete', {method:'POST', body: JSON.stringify({name})});
    toast('已删除'); if (SCR===name){ SCR=''; $('scrEdit').value=''; } loadScripts();
  }catch(e){ toast(e.message, true); }
}

async function loadLogList(){
  try{
    const d = await api('/api/logs');
    const sel = $('logSel');
    sel.innerHTML = d.items.map(f=>`<option value="${f.name}">${f.name} (${f.size} B)</option>`).join('');
    if (sel.options.length) loadLog();
  }catch(e){ toast(e.message, true); }
}
async function loadLog(){
  const name = $('logSel').value; if (!name) return;
  try{ const d = await api('/api/log?name='+encodeURIComponent(name)+'&lines='+($('logLines').value||200));
    $('logOut').textContent = d.text || '(空)';
  }catch(e){ toast(e.message, true); }
}

async function init(){
  if (!TOKEN) return showLogin();
  $('login').classList.add('hide');
  loadSvc(); loadScripts();
}
$('tok').addEventListener('keydown', e => { if (e.key==='Enter') login(); });
init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 启动
def main():
    parser = argparse.ArgumentParser(description="NapCatQQ-dokcer 网页管理面板")
    parser.add_argument("--host", default=None, help="监听地址, 默认取 conf/web_panel.json")
    parser.add_argument("--port", type=int, default=None, help="监听端口, 默认 8090")
    parser.add_argument("--token", default=None, help="临时覆盖访问令牌")
    parser.add_argument("--reset-token", action="store_true", help="重新生成访问令牌")
    args = parser.parse_args()

    cfg = load_panel_config()
    if args.reset_token:
        cfg["token"] = secrets.token_urlsafe(12)
        save_json(PANEL_CONF, cfg, mode=0o600)
    if args.token:
        cfg["token"] = args.token
    host = args.host or cfg.get("host") or "0.0.0.0"
    port = int(args.port or cfg.get("port") or 8090)

    Handler.cfg = {"token": cfg["token"]}
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[面板] 已启动: http://{host}:{port}/", flush=True)
    print(f"[面板] 访问令牌: {cfg['token']}", flush=True)
    print(f"[面板] 首次访问可用 http://{host}:{port}/?token={cfg['token']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[面板] 已停止", flush=True)


if __name__ == "__main__":
    main()
