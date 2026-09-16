#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_spark.py — 抖音「续火花」守护脚本（无头浏览器 + QQ群@管理 + 每日 00:00:01 定时发送）

工作流程:
  1. 先用 douyin_qrlogin.py 扫码登录, 登录态存 douyin_state.json;
  2. 启动本脚本后, 它会获取「自己的抖音号」, 并对 spark_list.json 名单里的每个抖音号
     在网页版消息页(https://www.douyin.com/chat)进行搜索, 与接口返回的账号做
     unique_id / short_id 精确比对, 比对通过才认为匹配成功, 资料写入 spark_friends.json;
  3. 每天 00:00:01 给所有「已匹配」的好友逐个发送续火花消息(随机文案+随机间隔);
  4. QQ 群里 @机器人 可以管理名单(仅管理员, 与 listener.py 共用 admins.json)。

QQ 指令(@机器人 + 文字, 关键词都在下面配置区, 可自行修改):
  @机器人 添加续火花 抖音号     例: 添加续火花 dy_abc123
  @机器人 删除续火花 抖音号     例: 删除续火花 dy_abc123
  @机器人 续火花名单            查看名单与匹配状态
  @机器人 立即续火花            立刻跑一轮(不等零点)
  @机器人 续火花帮助            查看用法

文件说明(分类存放: 配置在 conf/, 日志在 logs/, 图片在 images/):
  conf/douyin_state.json        登录态(由 douyin_qrlogin.py 生成, 勿泄露)
  conf/spark_list.json          需续火花的抖音号名单(@增删, 本脚本维护)
  conf/spark_friends.json       匹配到的账号资料+发送状态(自动生成, spark_status.py 读取)
  logs/douyin_spark.log         运行日志

命令行:
  python3 douyin_spark.py              常驻模式(WS监听 + 零点定时)
  python3 douyin_spark.py --check      只检查登录态
  python3 douyin_spark.py --match 抖音号  立刻匹配一个号并写入资料
  python3 douyin_spark.py --fire       立刻跑一轮发送
  python3 douyin_spark.py --days       只刷新火花天数(扫一次会话列表, 不发消息)
  python3 douyin_spark.py --selftest   打开消息页导出调试信息(抖音改版时排查选择器)

依赖: playwright + chromium(见 douyin_qrlogin.py 头部安装说明)
⚠️ 自动化发消息有风控风险, 请仅用于个人账号, 频率参数已内置且可调, 后果自负。
"""

import os
import re
import sys
import json
import time
import fcntl
import random
import socket
import base64
import struct
import hashlib
import logging
import argparse
import threading
import urllib.request
import resource
from datetime import datetime

# ============ 内存限制 ============
# 浏览器脚本不设 RLIMIT_AS(V8 预映射大量虚拟地址空间, 设了会 OOM);
# 实际 JS 堆由 Chromium --max-old-space-size=128 严格控制
# Python 进程本身仅占约 20MB, 无需额外限制

# ==================== 配置区(@功能/定时/文案都在这里改) ====================
# 目录结构(脚本在 scripts/, 配置/状态在 conf/, 日志在 logs/, 图片在 images/):
BASE_DIR = os.path.dirname(os.path.abspath(__file__))    # scripts/ 目录
ROOT_DIR = os.path.dirname(BASE_DIR)                     # 项目根 NapCatQQ-dokcer/
CONF_DIR  = os.path.join(ROOT_DIR, "conf")
LOG_DIR   = os.path.join(ROOT_DIR, "logs")
IMAGE_DIR = os.path.join(ROOT_DIR, "images")

# ---- 文件 ----
PROFILE_DIR   = os.path.join(CONF_DIR, "douyin_profile")  # 持久化浏览器配置(与 qrlogin 共用)
STATE_FILE    = os.path.join(CONF_DIR, "douyin_state.json")
LIST_FILE     = os.path.join(CONF_DIR, "spark_list.json")
FRIENDS_FILE  = os.path.join(CONF_DIR, "spark_friends.json")
LOCK_FILE     = os.path.join(BASE_DIR, ".douyin_spark.lock")  # 进程锁(放 scripts/ 即可)
ADMIN_FILE    = os.path.join(CONF_DIR, "admins.json")     # 与 listener.py 共用
LOG_FILE      = os.path.join(LOG_DIR, "douyin_spark.log")
DEBUG_PNG     = os.path.join(IMAGE_DIR, "douyin_chat_debug.png")

# ---- NapCat 连接(与 listener.py 保持一致) ----
WS_HOST, WS_PORT, WS_TOKEN = "127.0.0.1", 3001, "napcat"
HTTP_BASE, HTTP_TOKEN      = "http://127.0.0.1:3000", "napcat"
BOT_QQ      = 1000000001          # 机器人自身 QQ(判断是否被@)
SUPER_ADMIN = 1000000002          # 超管 QQ
ALLOW_GROUPS = []                 # 响应哪些群; 空=所有群
REPORT_GROUPS = []                # 零点发送完成后, 把汇总发到这些群; 空=只写日志

# ---- @指令关键词(想换说法只改这里) ----
KW_ADD  = ["添加续火花", "增加续火花", "新增续火花"]
KW_DEL  = ["删除续火花", "移除续火花", "取消续火花"]
KW_LIST = ["续火花名单", "续火花列表", "火花名单"]
KW_NOW  = ["立即续火花", "马上续火花", "手动续火花"]
KW_HELP = ["续火花帮助", "火花帮助"]
KW_REFRESH = ["刷新火花", "火花天数刷新"]   # 一键刷新全部火花天数(扫会话列表)

# ---- 续火花行为 ----
FIRE_HH, FIRE_MM, FIRE_SS = 0, 0, 1      # 每天发送时刻: 00:00:01
PREWARM_SEC  = 45                         # 提前多少秒打开浏览器/消息页待命
FRIEND_GAP   = (8, 75)                    # 每个好友之间随机间隔(秒), 防风控
DAILY_LIMIT  = 50                         # 每轮最多发送人数(防异常刷屏)
HEADLESS     = True                       # 无头模式; 调试选择器可临时改 False
SPARK_MESSAGES = [                        # 发送文案, 每轮每人随机挑一条
    "在吗～续个火花🔥",
    "滴！火花续命🔥",
    "来续火花啦，别让它灭了～",
    "晚安晚安，续个火花再睡🌙🔥",
    "火花火花，冲冲冲🔥",
]
CHAT_URL     = "https://www.douyin.com/chat"
USER_AGENT   = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}

# ---- 页面选择器(抖音前端改版时主要改这里) ----
# 会话/好友搜索框候选
SEARCH_INPUT_SELECTORS = [
    "input[placeholder*='搜索']",
    "aside input[type='text']",
    "[class*='sidebar'] input",
    "[class*='session'] input",
    "[data-e2e*='search'] input",
]
# 搜索结果项候选(用于点选第一条)
SEARCH_RESULT_SELECTORS = [
    "[class*='search-result'] [class*='item']",
    "[class*='searchResult'] [class*='item']",
    "[class*='result'] li",
    "[data-e2e*='search'] [class*='item']",
]
# 消息输入框候选(实测: 抖音聊天页输入框 class 含 messageEditorinputArea, data-placeholder=发送消息)
EDITOR_SELECTORS = [
    "div.messageEditorinputArea[contenteditable='true']",
    "div[data-e2e='im-chat-input-editor']",
    "div[contenteditable='true'][data-e2e*='input']",
    "div[contenteditable='true'][data-placeholder*='发送']",
    "div[contenteditable='true']",
]
# 火花天数文案匹配(聊天页头部/会话项里的 “火花 N天 / 连续聊天N天”)
SPARK_DAY_PATTERNS = [
    r"火花[^\d]{0,10}(\d{1,5})\s*天",
    r"连续聊天\s*(\d{1,5})\s*天",
    r"(\d{1,5})\s*天\s*火花",
]
# ========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("douyin_spark")

# 浏览器任务串行化(WS线程的临时任务 vs 零点任务)
BROWSER_LOCK = threading.RLock()
# WS 任务队列(单 worker 串行处理)
_JOB_QUEUE = None


class LoginRequired(RuntimeError):
    pass


# ---------------- JSON 存储(带文件锁, 跨线程/进程安全) ----------------
def _io_lock(path):
    fp = open(path + ".lock", "w")
    fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
    return fp


def load_list():
    """返回名单: [{'douyin_id':..., 'nickname':..., 'remark':..., 'added_by':..., 'added_at':...}]
    douyin_id 为空时用 nickname 去重, 保证按昵称续火花也能正常加载。"""
    try:
        with open(LIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        targets = data.get("targets", [])
        # 去重保序: 优先用 douyin_id, 为空则用 nickname
        seen, out = set(), []
        for t in targets:
            dy = str(t.get("douyin_id", "")).strip()
            nick = str(t.get("nickname", "")).strip()
            key = dy if dy else f"nick:{nick}"
            if key and key not in seen:
                seen.add(key)
                out.append({"douyin_id": dy,
                            "nickname": nick,
                            "remark": t.get("remark", ""),
                            "added_by": t.get("added_by"),
                            "added_at": t.get("added_at")})
        return out
    except FileNotFoundError:
        return []
    except Exception as e:
        log.info(f"名单读取失败: {e}")
        return []


def save_list(targets):
    fp = _io_lock(LIST_FILE)
    try:
        tmp = LIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"),
                       "targets": targets}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LIST_FILE)
    finally:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()


def load_friends():
    try:
        with open(FRIENDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"my_douyin_id": None, "my_nickname": None,
                "updated": None, "friends": {}}
    except Exception as e:
        log.info(f"匹配资料读取失败: {e}")
        return {"my_douyin_id": None, "my_nickname": None,
                "updated": None, "friends": {}}


def save_friends(data):
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    fp = _io_lock(FRIENDS_FILE)
    try:
        tmp = FRIENDS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FRIENDS_FILE)
    finally:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()


def upsert_friend(dy_id, fields):
    """更新某个抖音号的匹配/发送资料, 立即落盘。"""
    data = load_friends()
    fr = data.setdefault("friends", {}).setdefault(dy_id, {})
    fr["douyin_id"] = dy_id
    fr.update(fields)
    save_friends(data)


def load_admins():
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f).get("admins", [])}
    except Exception:
        return set()


def is_privileged(qq):
    return int(qq) == int(SUPER_ADMIN) or int(qq) in load_admins()


# ---------------- NapCat HTTP / WebSocket ----------------
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
        return None


class WSClient:
    """纯标准库 RFC6455 客户端(只收消息, 复用 listener.py 的极简实现)。"""
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
            f"Authorization: Bearer {self.token}",
        ]
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("握手时连接关闭")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WS 握手失败: {resp[:120]!r}")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept.encode() not in resp:
            log.info("[WS] Sec-WebSocket-Accept 不匹配(仍继续)")
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
            else:
                if fin:
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


# ---------------- 抖音网页自动化 ----------------
def _setup_page(page):
    def _route(route):
        if route.request.resource_type in ("font", "media"):
            return route.abort()
        return route.continue_()
    page.route("**/*", _route)


def _goto(page, url, timeout=45000):
    page.goto(url, wait_until="commit", timeout=timeout)


def _logged_in(context):
    try:
        names = {c.get("name") for c in context.cookies()}
        return bool(names & LOGIN_COOKIE_NAMES)
    except Exception:
        return False


def _iter_user_dicts(obj, out=None):
    """递归遍历接口 JSON, 把像「用户资料」的 dict 都收集出来。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        keys = set(obj.keys())
        if "nickname" in keys and ({"unique_id", "short_id", "sec_uid", "uid"} & keys):
            out.append(obj)
        for v in obj.values():
            _iter_user_dicts(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _iter_user_dicts(v, out)
    return out


class DouyinSession:
    """一次浏览器会话的上下文管理器。"""
    def __init__(self):
        self.p = self.browser = self.context = self.page = None

    def __enter__(self):
        if not os.path.exists(STATE_FILE):
            raise LoginRequired(
                "缺少 douyin_state.json, 请先在服务器运行: python3 douyin_qrlogin.py")
        from playwright.sync_api import sync_playwright
        self.p = sync_playwright().start()
        self.browser = self.p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--lang=zh-CN",
                  "--memory-pressure-off",
                  "--disable-extensions",
                  "--disable-plugins",
                  "--disable-component-extensions-with-background-pages",
                  "--disable-background-networking",
                  "--js-flags=--max-old-space-size=128"])
        self.context = self.browser.new_context(
            storage_state=STATE_FILE,
            user_agent=USER_AGENT, locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900})
        self.context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self.page = self.context.new_page()
        _setup_page(self.page)
        return self

    def __exit__(self, *exc):
        try:
            self.context and self.context.close()
        except Exception:
            pass
        try:
            self.browser and self.browser.close()
        except Exception:
            pass
        try:
            self.p and self.p.stop()
        except Exception:
            pass
        return False

    def require_login(self):
        if not _logged_in(self.context):
            raise LoginRequired("登录态已失效, 请重新运行: python3 douyin_qrlogin.py")

    def get_self_info(self):
        """打开个人主页, 拦截 profile 接口拿自己的抖音号/昵称。"""
        info = {}

        def _on(r):
            if "/aweme/v1/web/user/profile/" in r.url:
                try:
                    j = r.json()
                    if isinstance(j, dict) and j.get("user"):
                        info.update(j["user"])
                except Exception:
                    pass
        self.page.on("response", _on)
        _goto(self.page, "https://www.douyin.com/user/self")
        for _ in range(12):
            self.page.wait_for_timeout(1000)
            if info:
                break
        dy_id = str(info.get("unique_id") or info.get("short_id") or "").strip()
        return dy_id, info.get("nickname")

    def open_chat(self, timeout_sec=25):
        """打开消息页并等到会话列表出现(不点搜索框, 避免破坏会话列表视图)。"""
        # 整个会话只注册一次 response 监听, 避免多次搜索时监听器堆积
        self._api_users = []

        def _collect(resp):
            u = resp.url
            # IM 搜索/会话列表类接口(让页面自己发签名请求, 我们只读取结果)
            if "/aweme/v1/web/im/" in u or "/search/" in u:
                try:
                    _iter_user_dicts(resp.json(), self._api_users)
                except Exception:
                    pass
        self.page.on("response", _collect)

        _goto(self.page, CHAT_URL)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            self.require_login()
            # 等会话列表出现(不点搜索框! 点击搜索框会把页面切到搜索视图, 隐藏会话列表)
            loc = self.page.locator("[data-e2e='conversation-item'], "
                                    "[class*='conversationConversationItemwrapper']")
            if loc.count() and loc.first.is_visible():
                # 会话列表已加载, 搜索框也会同时存在, 这里只确认页面就绪即可
                return "session-list"
            self.page.wait_for_timeout(1500)
        # 没等到也继续(选择器可能失效), 交给调用方报错/截图
        self.page.screenshot(path=DEBUG_PNG)
        raise RuntimeError(f"消息页未找到会话列表, 截图: {DEBUG_PNG}")

    def _find_search_box(self):
        for sel in SEARCH_INPUT_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
        return None

    def search_friend(self, query, target_id=None):
        """在消息页搜索框输入 query, 拦截接口返回并做精确比对。
        ⚠️ 实测: IM 搜索框只支持按昵称/聊天内容搜, 搜抖音号是搜不到人的!
        所以正确用法是「按昵称搜, 再用 target_id(抖音号) 精确比对 unique_id/short_id」。
        target_id 为 None 时: 依次用 query 比对 ID, 再用昵称精确比对(适用于名单只有昵称)。
        返回 (匹配到的用户dict 或 None, 本次搜到的候选数)。"""
        box = self._find_search_box()
        if box is None:
            raise RuntimeError("找不到搜索框")

        self._api_users.clear()
        box.click()
        try:
            box.fill("")
        except Exception:
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
        # 拟人逐字输入
        self.page.keyboard.type(query, delay=random.randint(120, 260))
        self.page.wait_for_timeout(3500)

        # 去重(同一用户可能被多个接口返回)
        uniq = {}
        for u in self._api_users:
            uid = str(u.get("uid") or u.get("sec_uid") or u.get("nickname"))
            if uid and uid not in uniq:
                uniq[uid] = u
        candidates = list(uniq.values())
        target = str(target_id or query).strip()
        for u in candidates:
            uid_custom = str(u.get("unique_id") or "").strip()
            uid_short = str(u.get("short_id") or "").strip()
            if uid_custom == target or (uid_short and uid_short == target):
                return u, len(candidates)
        # 纯数字目标: 也尝试与内部 uid 比对(有些用户填的「抖音号」其实是 uid)
        if target.isdigit():
            for u in candidates:
                if str(u.get("uid") or "").strip() == target:
                    return u, len(candidates)
        # 未指定 target_id(名单只有昵称): 允许候选昵称精确匹配
        if target_id is None:
            q = query.strip()
            for u in candidates:
                if str(u.get("nickname") or "").strip() == q:
                    return u, len(candidates)
        return None, len(candidates)


    def find_in_session_list(self, nick):
        """从左侧会话列表按昵称匹配并点开, 返回是否成功打开输入框。
        实测: get_by_text(昵称) 能稳定点开抖音聊天页的会话, 优先用它。"""
        if not nick:
            return False
        try:
            # 先取消可能残留的搜索状态(点"取消"按钮, Escape 对抖音前端无效)
            try:
                cancel = self.page.locator("text=取消")
                if cancel.count() and cancel.first.is_visible():
                    cancel.first.click(timeout=1500)
                    self.page.wait_for_timeout(800)
            except Exception:
                pass
            # 优先用 get_by_text 点昵称(实测稳定)
            try:
                self.page.get_by_text(nick, exact=True).first.click(timeout=4000)
                self.page.wait_for_timeout(1500)
                if self._editor():
                    return True
            except Exception:
                pass
            # 兜底: 遍历会话列表项匹配
            wraps = self.page.locator(
                "[data-e2e='conversation-item'], "
                ".conversationConversationItemwrapper")
            n = wraps.count()
            for i in range(min(n, 80)):  # 只看前80个会话项
                try:
                    item = wraps.nth(i)
                    if not item.is_visible():
                        continue
                    # 取会话项的昵称
                    title_el = item.locator(
                        ".conversationConversationItemtitle, "
                        ".conversationConversationItemtitleWrapper")
                    title_txt = ""
                    try:
                        title_txt = title_el.first.inner_text(timeout=1000).strip()
                    except Exception:
                        title_txt = item.inner_text(timeout=1000).strip()
                    # 昵称匹配: 会话项的 title 第一行就是昵称
                    if title_txt and (title_txt.startswith(nick) or nick in title_txt):
                        item.click(timeout=3000)
                        self.page.wait_for_timeout(1500)
                        if self._editor():
                            return True
                except Exception:
                    continue
            # 最后兜底: 模糊匹配
            try:
                self.page.get_by_text(nick, exact=False).first.click(timeout=3000)
                self.page.wait_for_timeout(1500)
                if self._editor():
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    def click_friend(self, user):
        """点开与该用户的会话。优先按昵称点搜索结果, 再兜底回车/第一条。"""
        nick = (user or {}).get("nickname")
        if nick:
            try:
                self.page.get_by_text(nick, exact=True).first.click(timeout=4000)
                self.page.wait_for_timeout(1500)
                if self._editor():
                    return True
            except Exception:
                pass
        # 兜底: 回车打开高亮的第一条结果
        try:
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(1500)
            if self._editor():
                return True
        except Exception:
            pass
        for sel in SEARCH_RESULT_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    loc.first.click(timeout=3000)
                    self.page.wait_for_timeout(1200)
                    if self._editor():
                        return True
            except Exception:
                continue
        return bool(self._editor())

    def _editor(self):
        """返回当前可见的消息输入框 locator(取最宽的 contenteditable)。"""
        best = None
        best_w = 0
        for sel in EDITOR_SELECTORS:
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if box and box["width"] > best_w:
                        best, best_w = el, box["width"]
            except Exception:
                continue
        return best

    def send_message(self, text):
        """在当前会话发送一条消息。返回 (是否成功, 说明)。"""
        editor = self._editor()
        if editor is None:
            return False, "找不到输入框"
        send_resp = {}

        def _on(resp):
            u = resp.url
            if "/im/" in u and "send" in u:
                try:
                    send_resp.update(resp.json())
                except Exception:
                    pass
        self.page.on("response", _on)

        try:
            editor.click()
            self.page.wait_for_timeout(300)
            self.page.keyboard.type(text, delay=random.randint(60, 140))
            self.page.wait_for_timeout(300)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(3500)

            # 判定1: IM 发送接口返回成功
            sc = send_resp.get("status_code")
            if sc in (0, None) and send_resp:
                return True, "接口返回成功"
            # 判定2: 最后一条消息气泡包含所发文本
            try:
                body = self.page.inner_text("body", timeout=2000)
                if text.split("🔥")[0].strip()[:8] in body:
                    return True, "气泡可见"
            except Exception:
                pass
            return False, f"发送可能失败 status_code={sc}"
        finally:
            # 避免多次 send_message 时 response 监听器堆积
            try:
                self.page.remove_listener("response", _on)
            except Exception:
                pass

    def read_spark_days(self, nick=None):
        """按昵称读取单个好友的火花天数, 读不到返回 None。
        简化说明(2026-09-17): 旧版只认 .commonStreaknormalText(蓝色小火苗样式),
        火焰其他样式(大火苗/hot 变体)类名不同, 导致 9 人只读到 1 人。
        现改为: ① 通用选择器 [class*='treak'](命中任意 Streak 样式)
               ② 兜底「会话项内任意纯数字行」(火花图标旁的数字)。
        """
        if nick:
            try:
                wraps = self.page.locator(
                    "[data-e2e='conversation-item'], "
                    ".conversationConversationItemwrapper")
                n = wraps.count()
                for i in range(min(n, 80)):
                    item = wraps.nth(i)
                    if not item.is_visible():
                        continue
                    try:
                        title_txt = item.locator(
                            ".conversationConversationItemtitle").first.inner_text(
                            timeout=800).strip()
                    except Exception:
                        continue
                    if title_txt and _nick_match(title_txt, nick):
                        days = self._days_from_item(item)
                        if days:
                            return days
            except Exception:
                pass
        # 兜底: 页面文本里的 "火花N天 / 连续聊天N天" 类文案
        try:
            text = self.page.inner_text("body", timeout=2000)
        except Exception:
            return None
        for pat in SPARK_DAY_PATTERNS:
            m = re.search(pat, text)
            if m:
                return int(m.group(1))
        return None

    def read_all_spark_days(self):
        """一次性扫描会话列表, 返回 {会话昵称: 火花天数}。
        只读列表不点开任何人 —— 一次页面加载即可刷新全部好友, 无需逐个查询。"""
        out = {}
        try:
            wraps = self.page.locator(
                "[data-e2e='conversation-item'], "
                ".conversationConversationItemwrapper")
            n = wraps.count()
            for i in range(min(n, 80)):
                item = wraps.nth(i)
                if not item.is_visible():
                    continue
                try:
                    title = item.locator(
                        ".conversationConversationItemtitle").first.inner_text(
                        timeout=800).strip()
                except Exception:
                    continue
                if not title:
                    continue
                days = self._days_from_item(item)
                if days:
                    out[title] = days
        except Exception as e:
            log.info(f"批量扫描火花天数失败: {e}")
        return out

    @staticmethod
    def _days_from_item(item):
        """从单个会话项抠出火花天数: ① class 含 treak 的元素(任意火焰样式)
        ② 兜底「纯数字行」。都没有返回 None。"""
        try:
            loc = item.locator("[class*='treak']")
            for k in range(min(loc.count(), 4)):
                txt = loc.nth(k).inner_text(timeout=500).strip()
                m = re.search(r"\d{1,5}", txt)
                if m:
                    return int(m.group())
        except Exception:
            pass
        try:
            txt = item.inner_text(timeout=800)
            for line in txt.splitlines():
                line = line.strip()
                if re.fullmatch(r"\d{1,5}", line):
                    return int(line)
        except Exception:
            pass
        return None

    def selftest(self):
        """打开消息页导出调试信息(抖音改版时用)。"""
        self.require_login()
        self.open_chat()
        self.page.wait_for_timeout(3000)
        report = self.page.evaluate(
            """() => {
              const vis = e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; };
              return {
                url: location.href,
                inputs: [...document.querySelectorAll('input')].filter(vis).map(e=>({
                  ph:e.placeholder, cls:String(e.className).slice(0,80)})).slice(0,10),
                editables: [...document.querySelectorAll('[contenteditable=true]')].filter(vis)
                  .map(e=>({cls:String(e.className).slice(0,80),
                            e2e:e.getAttribute('data-e2e')})).slice(0,10),
              };
            }""")
        self.page.screenshot(path=DEBUG_PNG)
        return report


# ---------------- 业务任务 ----------------
def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _find_list_nickname(dy_id):
    for t in load_list():
        if t.get("douyin_id") == dy_id and t.get("nickname"):
            return t["nickname"]
    return ""


def job_match_one(dy_id, nickname="", session=None):
    """匹配名单中的一个抖音号, 写 spark_friends.json。返回 (ok, msg)。
    匹配策略(按可靠性排序, 实测结果):
      1) 消息页左侧会话列表按昵称查找并点开 —— 最稳, 目标和机器人聊过就一定能找到;
      2) IM 搜索框按昵称搜索, 用抖音号精确比对 unique_id/short_id —— 强校验;
      3) IM 搜索框直接搜抖音号 —— 基本搜不到(搜索框不支持抖音号), 仅最后兜底。
    ⚠️ 消息页搜索框不支持按抖音号搜人; 从未和机器人聊过的陌生人无法匹配。"""
    if session is not None:
        return _match_in_session(session, dy_id, nickname)
    with DouyinSession() as s:
        return _match_in_session(s, dy_id, nickname)


def _match_in_session(s, dy_id, nickname=""):
    s.require_login()
    if not getattr(s, "_self_done", False):
        my_id, my_nick = s.get_self_info()
        if my_id:
            fr = load_friends()
            fr["my_douyin_id"], fr["my_nickname"] = my_id, my_nick
            save_friends(fr)
        s._self_done = True
    s.open_chat()
    nick = nickname or _find_list_nickname(dy_id)
    key = dy_id  # 名单/friends 的 key 约定: 有抖音号用抖音号, 没有为空串(靠昵称)
    # 1) 会话列表按昵称查找(失败等 2.5s 重试一次, 防页面未就绪的瞬时竞态)
    opened = nick and s.find_in_session_list(nick)
    if not opened and nick:
        s.page.wait_for_timeout(2500)
        opened = s.find_in_session_list(nick)
    if opened:
        days = s.read_spark_days(nick)
        upsert_friend(key, {
            "matched": True, "matched_at": _now_str(),
            "nickname": nick, "match_method": "session_list",
            "spark_days": days, "last_error": None})
        tail = f"\n火花天数: {days}" if days is not None else ""
        return True, (f"✅ 已匹配 {dy_id or nick}\n昵称: {nick}{tail}\n方式: 会话列表")
    # 2) IM 搜索: 按昵称搜 + 抖音号精确比对
    user, n_cand = None, 0
    if nick:
        user, n_cand = s.search_friend(nick, target_id=dy_id or None)
    # 3) 兜底: 直接搜抖音号(实测基本搜不到)
    if user is None and dy_id:
        try:
            user, n2 = s.search_friend(dy_id, target_id=dy_id)
            n_cand = n_cand or n2
        except Exception:
            pass
    if user is not None:
        clicked = s.click_friend(user)
        days = s.read_spark_days(nick or (user or {}).get("nickname")) if clicked else None
        info = {
            "matched": True,
            "matched_at": _now_str(),
            "nickname": user.get("nickname"),
            "uid": str(user.get("uid") or ""),
            "sec_uid": str(user.get("sec_uid") or ""),
            "unique_id": str(user.get("unique_id") or ""),
            "short_id": str(user.get("short_id") or ""),
            "match_method": "im_search",
            "spark_days": days,
            "last_error": None if clicked else "已匹配但未打开会话, 零点将重试",
        }
        upsert_friend(key, info)
        tail = f"\n火花天数: {days}" if days is not None else ""
        if not clicked:
            tail += "\n(账号已匹配, 但没能打开会话, 零点会自动重试)"
        return True, f"✅ 已匹配 {dy_id or nick}\n昵称: {info['nickname']}{tail}"
    upsert_friend(key, {
        "matched": False, "matched_at": _now_str(),
        "last_error": f"会话列表与搜索均未找到(昵称:{nick or '无'})"
                      f", 若是新好友请先互发一条消息"})
    return False, (f"❌ 未匹配到 {dy_id or nick}"
                   f"\n(会话列表和搜索都找不到, 对方需先和机器人互发过消息)")


def job_match_all():
    """重新匹配名单里所有抖音号(不发消息)。
    每 3 个目标重建一次浏览器会话: 实测反复导航后 Chromium 小内存堆会崩(Page crashed)。"""
    targets = load_list()
    if not targets:
        return "名单为空"

    def _close(s):
        try:
            s.__exit__(None, None, None)
        except Exception:
            pass

    results, attempts = [], {}
    i, sess, used = 0, None, 0
    while i < len(targets):
        t = targets[i]
        dy, nick = t.get("douyin_id", ""), t.get("nickname", "")
        label = dy or ("昵称:" + nick)
        attempts[label] = attempts.get(label, 0) + 1
        if attempts[label] > 2:   # 同一目标最多重试一次, 防死循环
            results.append(f"❌ {label} (连续异常, 跳过)")
            i += 1
            continue
        if sess is None:
            try:
                sess = DouyinSession()
                sess.__enter__()
                used = 0
            except Exception as e:
                results.append(f"❌ {label} (会话创建失败: {e})")
                _close(sess)
                sess = None
                i += 1
                continue
        crashed = False
        try:
            ok, msg = _match_in_session(sess, dy, nick)
        except Exception as e:
            ok, msg = False, f"❌ {label} 异常: {e}"
            if "crash" in str(e).lower():
                crashed = True
        results.append(f"{'✅' if ok else '❌'} {label}")
        log.info(f"[matchall] {label} -> {msg.splitlines()[0]}")
        i += 1
        if crashed:
            _close(sess)
            sess = None
            continue
        used += 1
        if used >= 3:
            _close(sess)
            sess = None
        else:
            try:
                sess.page.goto(CHAT_URL, wait_until="commit", timeout=45000)
                sess.page.wait_for_timeout(2000)
            except Exception:
                _close(sess)
                sess = None
    if sess is not None:
        _close(sess)
    ok_n = sum(1 for r in results if r.startswith("✅"))
    return f"🔎 重新匹配完成 {ok_n}/{len(results)}\n" + "\n".join(results)


def _nick_match(title, nick):
    """昵称宽松匹配: 会话列表 title 与资料昵称互为包含即算命中
    (表情/后缀差异, 如「👁️‍🗨️小选」vs「小选」)。"""
    if not title or not nick:
        return False
    return (title == nick or title.startswith(nick) or nick.startswith(title)
            or nick in title or title in nick)


def _scan_and_update_days(sess):
    """核心刷新: 在已打开的消息页上扫一遍会话列表, 更新 spark_friends.json。
    供 --days 命令与每轮发送前调用(页面由调用方准备)。返回汇总文本。"""
    day_map = sess.read_all_spark_days()
    if not day_map:
        return "未读到任何火花天数(会话列表为空或抖音改版), 请跑 --selftest 排查"
    data = load_friends()
    updated, misses = [], []
    for dy_id, fr in data.get("friends", {}).items():
        nick = fr.get("nickname")
        if not nick or not fr.get("matched"):
            continue
        days = None
        for title, d in day_map.items():
            if _nick_match(title, nick):
                days = d
                break
        if days:
            fr["spark_days"] = days
            updated.append(f"{nick} {days}天")
        else:
            misses.append(nick)
    save_friends(data)
    log.info(f"火花天数刷新: {len(updated)} 个更新, {len(misses)} 个未读到")
    lines = [f"🔥 火花天数刷新完成 {len(updated)}/{len(updated) + len(misses)}"]
    lines += [f"• {u}" for u in updated]
    if misses:
        lines.append("未读到(会话列表里没有火花标记): " + "、".join(misses))
    return "\n".join(lines)


def refresh_all_days(sess=None):
    """一键刷新所有已匹配好友的火花天数: 只打开一次消息页, 扫一遍会话列表,
    按「标题↔昵称」匹配后写入 spark_friends.json。返回汇总文本。
    简化点: 全程不点开任何聊天窗, 9 人也只需一次页面加载。
    注意: DouyinSession 必须经 with 启动(构造函数不开浏览器)。"""
    if sess is not None:                      # 调用方已开好页面
        return _scan_and_update_days(sess)
    with DouyinSession() as s:
        s.require_login()
        s.open_chat()
        s.page.wait_for_timeout(2500)         # 等会话列表渲染完
        return _scan_and_update_days(s)


def job_run_daily(fire_epoch=None, report_groups=()):
    """跑一轮续火花。fire_epoch 给定时, 会先开浏览器待命, 到点(00:00:01)再发。"""
    targets = load_list()
    if not targets:
        log.info("名单为空, 本轮跳过")
        return "名单为空"
    targets = targets[:DAILY_LIMIT]
    results = []
    with DouyinSession() as s:
        s.require_login()
        my_id, my_nick = s.get_self_info()
        log.info(f"登录账号: 抖音号={my_id} 昵称={my_nick}")
        s.open_chat()
        # 开页后先一次性刷新全部火花天数(只扫会话列表, 不点开聊天窗, 不耗时)
        try:
            log.info(_scan_and_update_days(s).splitlines()[0])
        except Exception as e:
            log.info(f"火花天数批量刷新失败(不影响发送): {e}")
        if fire_epoch:
            # 已提前开好页面, 精确等到点(最后 50ms 忙等)
            while True:
                remain = fire_epoch - time.time()
                if remain <= 0:
                    break
                if remain > 0.05:
                    time.sleep(min(remain - 0.05, 1.0))
                # else 忙等
        friends = load_friends()
        friends["my_douyin_id"], friends["my_nickname"] = my_id, my_nick
        save_friends(friends)

        for idx, t in enumerate(targets):
            dy_id = t["douyin_id"]
            if idx > 0:
                gap = random.randint(*FRIEND_GAP)
                log.info(f"等待 {gap}s 再处理下一位...")
                time.sleep(gap)
            rec = friends.get("friends", {}).get(dy_id, {})
            try:
                opened = False
                # 优先按昵称走会话列表匹配(稳定可靠, 网页搜索常搜不到)
                nick_target = t.get("nickname") or rec.get("nickname")
                if nick_target:
                    opened = s.find_in_session_list(nick_target)
                    if opened:
                        # 会话列表匹配成功: 标记 matched=True (无需 unique_id)
                        upsert_friend(dy_id, {
                            "matched": True, "matched_at": _now_str(),
                            "nickname": nick_target,
                            "match_method": "session_list",
                            "last_error": None})
                        rec = load_friends().get("friends", {}).get(dy_id, {})
                # 未匹配过的(昵称兜底也没打开)再走 IM 搜索: 按昵称搜+抖音号比对
                # (实测搜索框不支持抖音号, 直接搜抖音号基本无效, 仅最后兜底)
                if not opened and not rec.get("matched"):
                    user, n_cand = None, 0
                    if nick_target:
                        try:
                            user, n_cand = s.search_friend(
                                nick_target, target_id=dy_id or None)
                        except Exception:
                            user, n_cand = None, 0
                    if user is None and dy_id:
                        try:
                            user, n2 = s.search_friend(dy_id, target_id=dy_id)
                            n_cand = n_cand or n2
                        except Exception:
                            pass
                    if user is None:
                        # 搜索失败后再尝试会话列表兜底(若名单有 nickname)
                        if nick_target:
                            opened = s.find_in_session_list(nick_target)
                            if opened:
                                upsert_friend(dy_id, {
                                    "matched": True, "matched_at": _now_str(),
                                    "nickname": nick_target,
                                    "match_method": "session_list_fallback",
                                    "last_error": None})
                                rec = load_friends().get("friends", {}).get(dy_id, {})
                        if not opened:
                            upsert_friend(dy_id, {"matched": False,
                                                 "last_error": f"候选{n_cand}个不匹配, 会话列表也未找到"})
                            results.append((dy_id, False, "未匹配"))
                            continue
                    else:
                        # user 匹配成功: 点开会话并记录资料
                        opened = s.click_friend(user)
                        upsert_friend(dy_id, {
                            "matched": True, "matched_at": _now_str(),
                            "nickname": user.get("nickname"),
                            "uid": str(user.get("uid") or ""),
                            "sec_uid": str(user.get("sec_uid") or ""),
                            "unique_id": str(user.get("unique_id") or ""),
                            "short_id": str(user.get("short_id") or ""),
                            "match_method": "im_search",
                            "last_error": None if opened else "账号已匹配但未能打开会话"})
                else:
                    # 已匹配: 优先走会话列表(更稳), 失败再走搜索
                    nick = rec.get("nickname") or t.get("nickname")
                    if nick:
                        opened = s.find_in_session_list(nick)
                    if not opened:
                        try:
                            user, _ = s.search_friend(nick or dy_id,
                                                      target_id=dy_id or None)
                        except Exception:
                            user = None
                        if user is not None:
                            opened = s.click_friend(user)
                    if not opened:
                        # 最后兜底: get_by_text
                        try:
                            if nick:
                                s.page.get_by_text(nick, exact=True).first.click(
                                    timeout=3000)
                                s.page.wait_for_timeout(1200)
                                opened = bool(s._editor())
                        except Exception:
                            pass

                # ⚠️ 只有确认打开了「目标好友」的会话才发送, 避免消息发错对象
                if not opened:
                    upsert_friend(dy_id,
                                  {"last_error": "未打开目标会话, 本轮跳过(未发送)"})
                    results.append((dy_id, False, "未打开会话, 已跳过"))
                    log.info(f"[{idx+1}/{len(targets)}] {dy_id} ❌ 未打开会话, 跳过不发送")
                    continue

                msg = random.choice(SPARK_MESSAGES)
                ok, why = s.send_message(msg)
                days = s.read_spark_days(
                    rec.get("nickname") or nick_target or t.get("nickname"))
                upsert_friend(dy_id, {
                    "last_send": _now_str(),
                    "last_message": msg,
                    "last_status": "ok" if ok else f"fail: {why}",
                    "spark_days": days,
                    "last_error": None if ok else why,
                })
                results.append((dy_id, ok, why))
                log.info(f"[{idx+1}/{len(targets)}] {dy_id} "
                         f"{'✅' if ok else '❌'} {why} 火花天数={days}")
            except Exception as e:
                upsert_friend(dy_id, {"last_send": _now_str(),
                                     "last_status": f"fail: {e}"})
                results.append((dy_id, False, str(e)))
                log.info(f"[{idx+1}/{len(targets)}] {dy_id} 异常: {e}")

    ok_n = sum(1 for _, ok, _ in results if ok)
    summary = f"🔥 续火花完成 {ok_n}/{len(results)}\n" + "\n".join(
        f"{'✅' if ok else '❌'} {dy} - {why}" for dy, ok, why in results)
    log.info(summary)
    for gid in report_groups:
        send_group(gid, summary)
    return summary


def next_fire_epoch(now=None):
    now = now or time.time()
    lt = time.localtime(now)
    target = time.mktime(time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, FIRE_HH, FIRE_MM, FIRE_SS, 0, 0, -1)))
    if target <= now:
        target += 86400
    return target


# ---------------- WS 指令 ----------------
RE_ADD = re.compile(r"(?:添加|增加|新增)续火花[\s+＋:：,，]*"
                    r"([A-Za-z0-9_.-]{2,24})(?:[\s,，。！!？?]|$)")
RE_DEL = re.compile(r"(?:删除|移除|取消)续火花[\s+＋:：,，]*"
                    r"([A-Za-z0-9_.-]{2,24})(?:[\s,，。！!？?]|$)")


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


def _short_time(s):
    """'2026-09-16 18:09:56' -> '18:09'(今天) / '09-15 22:10'(非今天), 空→''。"""
    if not s or s == "从未":
        return ""
    try:
        dt = datetime.fromisoformat(s)
        if dt.date() == datetime.now().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(s)[:10]


def format_list():
    """续火花名单: 每人一行的精简视图(与 spark_status 的查看火花保持同风格)。"""
    targets = load_list()
    if not targets:
        return "📋 续火花名单为空\n用法: @我 添加续火花 抖音号"
    store = load_friends()
    fr = store.get("friends", {})
    matched = sum(1 for t in targets if fr.get(t["douyin_id"], {}).get("matched"))
    lines = [f"📋 续火花 {matched}/{len(targets)}"]
    for t in targets:
        info = fr.get(t["douyin_id"], {})
        nick = (info.get("nickname") or t.get("nickname")
                or t["douyin_id"] or "?")
        if info.get("matched"):
            seg = f"🟢 {nick}"
            days = info.get("spark_days")
            if days is not None:
                seg += f" {days}天"
            last = _short_time(info.get("last_send"))
            if last:
                seg += f" {last}{'✅' if info.get('last_status') == 'ok' else '⚠️'}"
            lines.append(seg)
        else:
            err = info.get("last_error") or "未匹配"
            lines.append(f"🔴 {nick} {err}")
    if store.get("my_douyin_id"):
        lines.append(f"登录: {store.get('my_nickname') or store['my_douyin_id']}")
    lines.append(f"每日 {FIRE_HH:02d}:{FIRE_MM:02d}:{FIRE_SS:02d} 自动发送")
    return "\n".join(lines)


def handle_command(text, qq, group_id):
    """返回要回复的文本; None=不是本脚本的指令。"""
    if any(k in text for k in KW_LIST):
        return format_list()
    if any(k in text for k in KW_HELP):
        return ("🔥 续火花用法(仅管理员)\n"
                "添加续火花 抖音号 —— 加入名单并自动匹配\n"
                "删除续火花 抖音号 —— 移出名单\n"
                "续火花名单 —— 查看匹配/发送状态\n"
                "立即续火花 —— 立刻跑一轮\n"
                "刷新火花 —— 只刷新火花天数(不发消息)\n"
                "每日 00:00:01 自动发送")
    if any(k in text for k in KW_REFRESH):
        _JOB_QUEUE.enqueue({"kind": "days", "qq": qq, "group": group_id})
        return "🔄 已开始刷新火花天数(只扫列表不发消息), 完成后汇报"
    if any(k in text for k in KW_NOW):
        _JOB_QUEUE.enqueue({"kind": "fire", "qq": qq, "group": group_id})
        return "🚀 已开始执行一轮续火花, 完成后在本群汇报"
    if any(k in text for k in KW_ADD):
        m = RE_ADD.search(text)
        if not m:
            return "❌ 格式: @我 添加续火花 抖音号  (例: 添加续火花 dy_abc123)"
        dy = m.group(1)
        targets = load_list()
        if any(t["douyin_id"] == dy for t in targets):
            return f"ℹ️ {dy} 已在名单中"
        targets.append({"douyin_id": dy, "remark": "", "added_by": qq,
                        "added_at": _now_str()})
        save_list(targets)
        _JOB_QUEUE.enqueue({"kind": "match", "id": dy, "qq": qq, "group": group_id})
        return (f"✅ 已加入续火花名单: {dy}\n正在后台打开抖音匹配账号, 稍后在本群反馈结果\n"
                f"当前名单共 {len(targets)} 个")
    if any(k in text for k in KW_DEL):
        m = RE_DEL.search(text)
        if not m:
            return "❌ 格式: @我 删除续火花 抖音号  (例: 删除续火花 dy_abc123)"
        dy = m.group(1)
        targets = load_list()
        new = [t for t in targets if t["douyin_id"] != dy]
        if len(new) == len(targets):
            return f"ℹ️ {dy} 不在名单中\n当前名单: {[t['douyin_id'] for t in targets]}"
        save_list(new)
        return (f"🗑 已从续火花名单删除: {dy}\n剩余 {len(new)} 个"
                f"\n(匹配资料仍保留在 spark_friends.json, 重新添加无需再匹配)")
    return None


def on_event(ev):
    if ev.get("post_type") != "message" or ev.get("message_type") != "group":
        return
    group_id = ev.get("group_id")
    if ALLOW_GROUPS and group_id not in ALLOW_GROUPS:
        return
    qq = ev.get("user_id")
    text, at_self = extract_message(ev.get("message"))
    if not at_self:
        return
    if not is_privileged(qq):
        # 普通成员: 静默(不暴露该功能)
        return
    reply = handle_command(text, qq, group_id)
    if reply:
        time.sleep(1)
        send_group(group_id, f"[CQ:at,qq={qq}] {reply}")
        log.info(f"群{group_id} {qq}: {text[:30]} -> 已回复")


# ---------------- 任务队列 + WS 线程 ----------------
class JobQueue:
    def __init__(self):
        self.q = []
        self.cond = threading.Condition()

    def enqueue(self, job):
        with self.cond:
            self.q.append(job)
            self.cond.notify()

    def get(self):
        with self.cond:
            while not self.q:
                self.cond.wait()
            return self.q.pop(0)


def worker_loop():
    """单 worker 串行跑浏览器任务, 避免并发开浏览器。"""
    while True:
        job = _JOB_QUEUE.get()
        try:
            with BROWSER_LOCK:
                if job["kind"] == "match":
                    _, msg = job_match_one(job["id"])
                elif job["kind"] == "fire":
                    groups = [job["group"]] + [g for g in REPORT_GROUPS
                                               if g != job["group"]]
                    msg = job_run_daily(report_groups=groups)
                elif job["kind"] == "days":
                    msg = refresh_all_days()
                else:
                    continue
            send_group(job["group"], f"[CQ:at,qq={job['qq']}] {msg}")
        except LoginRequired as e:
            send_group(job["group"], f"[CQ:at,qq={job['qq']}] ⚠️ {e}")
        except Exception as e:
            log.info(f"任务异常: {e}")
            send_group(job["group"], f"[CQ:at,qq={job['qq']}] ❌ 任务异常: {e}")


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
                pass
            time.sleep(5)


# ---------------- 单实例锁 ----------------
def acquire_lock():
    fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp
    except BlockingIOError:
        return None


# ---------------- 主流程 ----------------
def daemon():
    global _JOB_QUEUE
    fp = acquire_lock()
    if fp is None:
        log.info("已有 douyin_spark 实例在运行, 退出")
        return

    _JOB_QUEUE = JobQueue()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()
    log.info("=" * 50)
    log.info(f"douyin_spark 启动 | bot={BOT_QQ} 名单={len(load_list())}个 "
             f"每日 {FIRE_HH:02d}:{FIRE_MM:02d}:{FIRE_SS:02d} 发送")

    while True:
        fire = next_fire_epoch()
        log.info(f"下一轮: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(fire))}")
        # 提前 PREWARM_SEC 开浏览器待命(在锁内打开, 到点即发)
        prewarm_at = fire - PREWARM_SEC
        while time.time() < prewarm_at:
            time.sleep(min(prewarm_at - time.time(), 5))
        try:
            with BROWSER_LOCK:
                job_run_daily(fire_epoch=fire, report_groups=list(REPORT_GROUPS))
        except LoginRequired as e:
            log.info(f"⚠️ {e}")
            for gid in REPORT_GROUPS:
                send_group(gid, f"⚠️ 抖音续火花未执行: {e}")
        except Exception as e:
            log.info(f"本轮异常: {e}")
        time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="抖音续火花守护")
    parser.add_argument("--check", action="store_true", help="只检查登录态")
    parser.add_argument("--match", metavar="抖音号", help="立刻匹配一个抖音号")
    parser.add_argument("--matchall", action="store_true",
                        help="重新匹配名单里所有抖音号(不发消息)")
    parser.add_argument("--fire", action="store_true", help="立刻跑一轮发送")
    parser.add_argument("--days", action="store_true",
                        help="只刷新所有好友的火花天数(扫一次会话列表, 不发消息)")
    parser.add_argument("--selftest", action="store_true", help="导出消息页调试信息")
    args = parser.parse_args()

    if args.check:
        try:
            with DouyinSession() as s:
                s.require_login()
                dy, nick = s.get_self_info()
                log.info(f"✅ 登录态有效 抖音号={dy} 昵称={nick}")
        except LoginRequired as e:
            log.info(f"❌ {e}")
            sys.exit(1)
        return

    if args.match:
        ok, msg = job_match_one(args.match)
        log.info(msg)
        sys.exit(0 if ok else 2)

    if args.matchall:
        log.info(job_match_all())
        return

    if args.fire:
        log.info(job_run_daily())
        return

    if args.days:
        log.info(refresh_all_days())
        return

    if args.selftest:
        with DouyinSession() as s:
            report = s.selftest()
            log.info("调试信息:\n" + json.dumps(report, ensure_ascii=False, indent=2))
            log.info(f"截图: {DEBUG_PNG}")
        return

    daemon()


if __name__ == "__main__":
    try:
        main()
    except ImportError as e:
        if "playwright" in str(e):
            log.info("❌ 缺少 playwright: pip3 install playwright && "
                     "python3 -m playwright install chromium")
            sys.exit(3)
        raise
