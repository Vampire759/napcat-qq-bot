#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qr_forward.py — 登录二维码自动转发到微信(QQ / 抖音)

监听两类登录二维码, 一刷新就通过飞牛 fnchat 的微信会话发给用户:
  • QQ   : NapCat 容器内 /app/napcat/cache/qrcode.png  (docker cp 出来)
  • 抖音 : images/douyin_qrcode.png / images/douyin_face_qrcode.png

发送失败(未装 fnchat / 未登录 / 网络异常)时:
  • 二维码仍会保存在 images/ 目录下(最新版 + 带时间戳的副本),
    日志里给出保存路径, 用户可自行取用。

配置:
  conf/fnchat.json   base_url / username / password / channel
  环境变量           FNCHAT_BASE / FNCHAT_USER / FNCHAT_PASS
                    QR_TARGET_USER(可选, 指定接收微信联系人 id)
                    NAPCAT_CONTAINER(默认 napcat)

用法:
  python3 scripts/qr_forward.py
  systemctl --user restart qr-forward
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONF_DIR = os.path.join(ROOT_DIR, "conf")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
IMAGE_DIR = os.path.join(ROOT_DIR, "images")
LOG_FILE = os.path.join(LOG_DIR, "qr_forward.log")
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [qr_forward] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("qr_forward")


def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or (default or {})
    except Exception:
        return default or {}


FN_CFG = _load_json(os.path.join(CONF_DIR, "fnchat.json"))
FNCHAT_BASE = os.environ.get("FNCHAT_BASE") or str(FN_CFG.get("base_url", "http://127.0.0.1:8033"))
FNCHAT_USER = os.environ.get("FNCHAT_USER") or str(FN_CFG.get("username", ""))
FNCHAT_PASS = os.environ.get("FNCHAT_PASS") or str(FN_CFG.get("password", ""))
QR_TARGET_USER = os.environ.get("QR_TARGET_USER") or None
NAPCAT_CONTAINER = os.environ.get("NAPCAT_CONTAINER") or "napcat"
QQ_QR_IN_CONTAINER = "/app/napcat/cache/qrcode.png"

POLL_SECONDS = 2.0

_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------- fnchat HTTP ----------------
class FnChat:
    def __init__(self):
        self.token = None
        self.target_user = QR_TARGET_USER

    def _request(self, method, path, data=None, raw_body=None, content_type=None,
                 timeout=20):
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = raw_body
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(FNCHAT_BASE + path, data=body,
                                     headers=headers, method=method)
        with _http.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def login(self):
        r = self._request("POST", "/api/user/login",
                          {"username": FNCHAT_USER, "password": FNCHAT_PASS},
                          timeout=10)
        self.token = r.get("token")
        if not self.token:
            raise RuntimeError(f"登录失败: {r}")
        log.info("fnchat 登录成功: %s", r.get("username"))

    def api(self, method, path, data=None, timeout=20):
        if not self.token:
            self.login()
        try:
            return self._request(method, path, data, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.login()
                return self._request(method, path, data, timeout=timeout)
            detail = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 fnchat {FNCHAT_BASE}: {e.reason}") from e

    def resolve_target(self):
        if self.target_user:
            return self.target_user
        try:
            convs = self.api("GET", "/api/conversations")
            if isinstance(convs, dict) and convs:
                best = max(convs.values(),
                           key=lambda v: v.get("last_message") or 0)
                self.target_user = best.get("user_id")
        except Exception as e:
            log.info("获取会话列表失败: %s", e)
        return self.target_user


def _multipart_post(fnchat, path, file_path, target_user):
    """multipart 上传(走 fnchat.api 的鉴权与重试)。"""
    boundary = "----qr" + uuid.uuid4().hex
    with open(file_path, "rb") as f:
        content = f.read()
    parts = []
    if target_user:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="target_user"'
            f'\r\n\r\n{target_user}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{os.path.basename(file_path)}"\r\n'
        f'Content-Type: image/png\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f'--{boundary}--\r\n'.encode())
    body = b"".join(parts)
    if not fnchat.token:
        fnchat.login()
    try:
        return fnchat._request("POST", "/api/upload-and-send",
                               raw_body=body,
                               content_type=f"multipart/form-data; boundary={boundary}",
                               timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fnchat.token = None
            fnchat.login()
            return fnchat._request("POST", "/api/upload-and-send",
                                   raw_body=body,
                                   content_type=f"multipart/form-data; boundary={boundary}",
                                   timeout=60)
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 fnchat {FNCHAT_BASE}: {e.reason}") from e


# ---------------- 二维码来源 ----------------
def qq_qr_mtime():
    """NapCat 容器内二维码的 mtime; 容器不在则返回 None。"""
    try:
        out = subprocess.run(
            ["docker", "exec", NAPCAT_CONTAINER, "stat", "-c", "%Y", QQ_QR_IN_CONTAINER],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return float(out.stdout.strip())
    except Exception:
        return None


def qq_qr_fetch(dest):
    try:
        out = subprocess.run(
            ["docker", "cp", f"{NAPCAT_CONTAINER}:{QQ_QR_IN_CONTAINER}", dest],
            capture_output=True, text=True, timeout=20)
        return out.returncode == 0 and os.path.getsize(dest) > 256
    except Exception as e:
        log.info("获取 QQ 二维码失败: %s", e)
        return False


def _snapshot(path):
    """保存带时间戳的副本(发送失败时供用户自行取用)。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    stem, ext = os.path.splitext(os.path.basename(path))
    dst = os.path.join(IMAGE_DIR, f"{stem}_{ts}{ext}")
    try:
        shutil.copyfile(path, dst)
        return dst
    except Exception as e:
        log.info("保存副本失败: %s", e)
        return None


def main():
    log.info("=" * 50)
    log.info("qr_forward 启动 | fnchat=%s images=%s", FNCHAT_BASE, IMAGE_DIR)

    fnchat = FnChat()
    logged_in = False
    try:
        fnchat.login()
        logged_in = True
    except Exception as e:
        log.info("fnchat 暂不可用(%s), 二维码将只保存到 %s", e, IMAGE_DIR)

    # 记录基线, 启动时不重发已经存在的旧二维码
    state = {
        "qq": qq_qr_mtime(),
        "douyin": (os.path.getmtime(os.path.join(IMAGE_DIR, "douyin_qrcode.png"))
                   if os.path.exists(os.path.join(IMAGE_DIR, "douyin_qrcode.png")) else None),
        "douyin_face": (os.path.getmtime(os.path.join(IMAGE_DIR, "douyin_face_qrcode.png"))
                        if os.path.exists(os.path.join(IMAGE_DIR, "douyin_face_qrcode.png")) else None),
    }

    def handle(kind, path):
        nonlocal logged_in
        if not logged_in:
            try:
                fnchat.token = None
                fnchat.login()
                logged_in = True
            except Exception as e:
                log.info("[%s] fnchat 不可用(%s), 二维码已保存: %s", kind, e, path)
                saved = _snapshot(path)
                if saved:
                    log.info("[%s] 副本: %s", kind, saved)
                return
        try:
            target = fnchat.resolve_target()
            r = _multipart_post(fnchat, "/api/upload-and-send", path, target)
            log.info("[%s] 二维码已发送到微信: %s", kind, r)
        except Exception as e:
            log.info("[%s] 发送失败(%s), 已保存到 images/: %s", kind, e, path)
            saved = _snapshot(path)
            if saved:
                log.info("[%s] 副本: %s", kind, saved)

    while True:
        # QQ 登录二维码
        m = qq_qr_mtime()
        if m is not None and m != state["qq"]:
            state["qq"] = m
            dst = os.path.join(IMAGE_DIR, "qq_login_qrcode.png")
            if qq_qr_fetch(dst):
                log.info("检测到新的 QQ 登录二维码")
                handle("QQ", dst)

        # 抖音登录二维码 / 刷脸二维码
        for kind, name in (("douyin", "douyin_qrcode.png"),
                           ("douyin_face", "douyin_face_qrcode.png")):
            p = os.path.join(IMAGE_DIR, name)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt != state[kind] and os.path.getsize(p) > 256:
                state[kind] = mt
                log.info("检测到新的抖音二维码: %s", name)
                handle(kind, p)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
