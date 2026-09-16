#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
listener.py — NapCat 群消息监听 + @机器人指令响应

功能:
  1. 通过 WebSocket 连接 NapCat, 记录群消息到 listener.log
  2. 当有人 @ 机器人时, 解析指令并回复。

  权限分层:
    • 普通人: 只能看到/使用「公开白名单」指令(PUBLIC_CMDS, 目前=抽签/一言, 可扩展),
              且每种指令每人每天各一次; 其余指令一律静默(当作不存在)。
    • 管理员/超管: 可用全部指令, 不受白名单与每日限次约束。
      发送「@我 手册」查看全部指令名(无描述)。

  常用指令:
       @机器人 菜单        -> 普通人可用指令(白名单)
       @机器人 手册        -> 全部指令名(仅管理员)
       @机器人 一言        -> 今日已打卡 + 每日一言
       @机器人 抽签        -> 每日一签(每人每天一次)
       @机器人 状态/时间    -> 运行状态/时间(仅管理员)
       @机器人 每日打卡     -> 给当前群打卡(仅管理员)
       @机器人 重启打卡     -> 重启 group_sign.py(仅管理员)
       @机器人 添加每日打卡 群号 -> 新增自动打卡群(仅管理员, 添加打卡群 为旧别名)
       @机器人 删除每日打卡 群号 -> 删除自动打卡群(仅管理员, 删除打卡群 为旧别名)
       @机器人 添加权限+QQ号  -> 添加管理员(仅超管)
       @机器人 群号 状态：开启/关闭 -> 单独开关某群的@功能(仅管理员)
                                  关闭后该群普通人@被忽略, 管理员仍可用

  WebSocket 客户端为纯标准库实现(RFC6455), 无需第三方依赖。

配置见下方「配置区」。
"""

import socket
import ssl  # noqa (保留, 便于将来接 wss)
import struct
import os
import re
import json
import time
import random
import base64
import hashlib
import subprocess
import logging
import urllib.request
import resource

# ============ 内存限制 ============
MEM_LIMIT_MB = 96
try:
    resource.setrlimit(resource.RLIMIT_AS,
                      (MEM_LIMIT_MB * 1024 * 1024, MEM_LIMIT_MB * 1024 * 1024))
except Exception:
    pass

# ============ 目录结构(项目按 5 类组织: 脚本/配置/日志/图片/测试) ============
# 本脚本位于 scripts/ 下; 所有配置、日志一律用下面的绝对路径常量,
# 不再依赖「当前工作目录」, 从任何地方启动行为都一致。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))    # scripts/ 目录(脚本自身所在)
ROOT_DIR = os.path.dirname(BASE_DIR)                     # 项目根 NapCatQQ-dokcer/
CONF_DIR = os.path.join(ROOT_DIR, "conf")                # 全部配置/状态 json 在 conf/
LOG_DIR  = os.path.join(ROOT_DIR, "logs")                # 全部日志在 logs/

# ============ 配置区 ============
WS_HOST      = "127.0.0.1"
WS_PORT      = 3001                 # NapCat websocketServer 端口
WS_TOKEN     = "napcat"             # ws token
HTTP_BASE    = "http://127.0.0.1:3000"
HTTP_TOKEN   = "napcat"
BOT_QQ       = 1000000001           # 机器人自身 QQ (用于判断是否被 @)
SUPER_ADMIN  = 1000000002           # 超级管理员: 永远可用所有指令, 且只有TA能开关"所有人@"
ADMIN_QQS    = []                   # 额外管理员QQ列表(可空); 与超管一样不受"所有人开关"限制
GROUPS_ALLOW = []                   # 允许响应的群(空=所有群); 如只在1000000003响应则填 [1000000003]

# "所有人@"开关: 默认状态 + 持久化文件(重启后保留上次状态)
ALLOW_EVERYONE_DEFAULT = True       # 首次运行时的默认值(True=所有人可@)
SWITCH_FILE  = os.path.join(CONF_DIR, "at_switch.json")      # 记录开关状态的文件
ADMIN_FILE   = os.path.join(CONF_DIR, "admins.json")         # 额外管理员QQ列表(超管用"添加权限+QQ号"动态增加, 持久化)
CHOUQIAN_FILE = os.path.join(CONF_DIR, "chouqian.json")      # 抽签「每人每天一次」记录(按 日期->已抽QQ集合)
CHOUQIAN_DELAY = 1.0                # 抽签后延迟多少秒再发送结果
# 打卡群列表: 与 group_sign.py 共享同一文件, 「添加打卡群」指令写入它, group_sign 零点读取。
SIGN_GROUPS_FILE = os.path.join(CONF_DIR, "sign_groups.json")
SEED_SIGN_GROUPS = [1000000003, 977249121, 959415637]  # 种子(与 group_sign.py 保持一致)

# 普通人(非管理员)可见/可用的指令关键词白名单(可后续扩展)。管理员不受此限制, 可用全部指令。
#   「当前占卜」= 随机起一卦(原占卜/卜卦/起卦, 现只保留一个入口);
#   「起卦」    = @我 起卦+问题, 按问题文本起卦。
PUBLIC_CMDS = ["抽签", "求签", "当前占卜", "起卦", "一言", "今日打卡"]
# 白名单里「不限每日次数」的公开指令(普通人也可反复使用, 不受"每天各一次"约束)。
PUBLIC_UNLIMITED = ["当前占卜", "起卦"]
PUBLIC_LIMIT_FILE = os.path.join(CONF_DIR, "public_limit.json")  # 普通人「每种指令每天各一次」记录
# 单群@开关: 被"关闭"的群, 普通人@被静默忽略, 管理员/超管仍可用。持久化。
GROUP_SWITCH_FILE = os.path.join(CONF_DIR, "group_switch.json")  # {"off": [群号, ...]} 记录被关闭的群

# 每日打卡连续天数(本地记录, 与"今日打卡"指令配套)。
STREAK_FILE  = os.path.join(CONF_DIR, "streak.json")  # {"QQ": {"last": "YYYY-MM-DD", "days": N}}

REPLY_DELAY  = 1.0                  # 所有@指令的回复统一延迟多少秒再发送

# ===== @冷却(防刷屏) =====
AT_WINDOW_SEC   = 10               # 判定窗口: 多少秒内连续@算"连续"
AT_MAX_IN_WIN   = 2                # 窗口内允许的@次数上限; 超过(第3次)即触发冷却
COOLDOWN_SEC    = 120 * 60         # 冷却时长: 120分钟
GROUP_COOLDOWN_MIN_USERS = 2       # 集体冷却: 窗口内有 >=N 个不同的人各自都超限, 则全群冷却
COOLDOWN_FILE   = os.path.join(CONF_DIR, "cooldown.json")  # 持久化: 个人/全群冷却截止时间 + "已提示"标记(永久只提示一次)

YIYAN_API    = "https://v.api.aa1.cn/api/yiyan/index.php"
SIGN_SCRIPT  = "group_sign.py"      # 打卡脚本文件名(与 listener 同在 scripts/ 目录)
SIGN_SERVICE = "napcat-sign.service"  # 打卡脚本的 systemd 用户服务名(重启走它, 保证全局唯一进程)
LISTENER_LOG = os.path.join(LOG_DIR, "listener.log")
SIGN_LOG     = os.path.join(LOG_DIR, "sign.log")

# 需要清理的 QQ/NapCat 日志目录 (按需增删; 相对路径一律转成基于项目根的绝对路径)
QQ_LOG_DIRS  = [
    os.path.expanduser("~/.config/QQ/nt_qq/log"),
    os.path.join(ROOT_DIR, "napcat", "logs"),
    os.path.join(ROOT_DIR, "napcat", "napcat", "logs"),
]
# ================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(LISTENER_LOG, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("listener")

START_TIME = time.time()


# ---------------- "所有人@"开关(持久化) ----------------
def load_allow_everyone():
    try:
        with open(SWITCH_FILE, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("allow_everyone", ALLOW_EVERYONE_DEFAULT))
    except Exception:
        return ALLOW_EVERYONE_DEFAULT


def save_allow_everyone(val):
    try:
        with open(SWITCH_FILE, "w", encoding="utf-8") as f:
            json.dump({"allow_everyone": bool(val)}, f)
    except Exception as e:
        log.info(f"[开关保存失败] {e}")


ALLOW_EVERYONE = load_allow_everyone()  # 运行时状态


# ---------------- 额外管理员列表(持久化) ----------------
def load_admins():
    """从文件读取额外管理员QQ列表(与代码里的 ADMIN_QQS 合并去重)。"""
    saved = []
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f).get("admins", [])
    except Exception:
        pass
    merged = {int(x) for x in ADMIN_QQS} | {int(x) for x in saved}
    return sorted(merged)


def save_admins(admins):
    try:
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump({"admins": [int(x) for x in admins]}, f)
    except Exception as e:
        log.info(f"[管理员保存失败] {e}")


ADMINS = load_admins()  # 运行时管理员列表(超管不在其中, 单独判断)


def is_privileged(qq):
    """超管或额外管理员: 不受"所有人开关"限制, 永远可用。"""
    return int(qq) == int(SUPER_ADMIN) or int(qq) in ADMINS


# ---------------- 纯标准库 WebSocket 客户端 ----------------
class WSClient:
    """极简 RFC6455 客户端, 仅支持 NapCat 本地明文 ws。"""
    def __init__(self, host, port, path="/", token=None):
        self.host, self.port, self.path, self.token = host, port, path, token
        self.sock = None
        self._buf = b""

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        req = "\r\n".join(headers) + "\r\n\r\n"
        self.sock.sendall(req.encode())
        # 读握手响应
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("握手时连接被关闭")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"握手失败: {resp[:120]!r}")
        # 校验 accept (可选)
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept.encode() not in resp:
            log.info("[WS] 警告: Sec-WebSocket-Accept 校验不匹配(仍继续)")
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
        """返回一条完整文本消息(自动处理分片/ping)。"""
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:      # close
                raise ConnectionError("服务器发送 close 帧")
            elif opcode == 0x9:    # ping -> pong
                self._send_frame(0xA, payload)
                continue
            elif opcode == 0xA:    # pong
                continue
            elif opcode in (0x1, 0x0):  # text / continuation
                if fin:
                    return payload.decode("utf-8", "replace")
                # 简化: NapCat 一般不分片, 分片则继续拼(此处从略)
                return payload.decode("utf-8", "replace")

    def _send_frame(self, opcode, data=b""):
        b1 = 0x80 | opcode
        length = len(data)
        mask = os.urandom(4)
        header = struct.pack("!B", b1)
        if length < 126:
            header += struct.pack("!B", 0x80 | length)
        elif length < 65536:
            header += struct.pack("!B", 0x80 | 126) + struct.pack("!H", length)
        else:
            header += struct.pack("!B", 0x80 | 127) + struct.pack("!Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + masked)

    def close(self):
        try:
            self._send_frame(0x8)
            self.sock.close()
        except Exception:
            pass


# ---------------- HTTP API 辅助 ----------------
def api(action, payload):
    req = urllib.request.Request(
        f"{HTTP_BASE}/{action}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {HTTP_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def send_group(group_id, text):
    try:
        return api("send_group_msg", {"group_id": int(group_id), "message": text})
    except Exception as e:
        log.info(f"[发送失败] {e}")
        return None


def fetch_yiyan():
    try:
        with urllib.request.urlopen(YIYAN_API, timeout=8) as r:
            text = r.read().decode("utf-8", "replace").strip()
        return re.sub(r"<[^>]+>", "", text).strip() or None
    except Exception as e:
        log.info(f"[一言失败] {e}")
        return None


# ---------------- NTP 偏差(复用简版) ----------------
def ntp_offset_ms():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
        s.sendto(b"\x1b" + 47 * b"\0", ("ntp.aliyun.com", 123))
        d, _ = s.recvfrom(48); s.close()
        t0 = time.time()
        tx_i, tx_f = struct.unpack("!II", d[40:48])
        t2 = (tx_i - 2208988800) + tx_f / 2**32
        return (t2 - t0) * 1000
    except Exception:
        return None


# ---------------- 指令实现 ----------------
def cmd_status():
    up = int(time.time() - START_TIME)
    h, rem = divmod(up, 3600); m, s = divmod(rem, 60)
    sign_running = is_sign_running()
    try:
        st = api("get_status", {}).get("data", {})
        online = st.get("online")
    except Exception:
        online = "未知"
    return (f"🤖 机器人状态\n"
            f"• QQ在线: {online}\n"
            f"• 监听运行: {h}h{m}m{s}s\n"
            f"• 打卡脚本: {'✅运行中' if sign_running else '❌未运行'}")


def cmd_time():
    off = ntp_offset_ms()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    off_str = f"{off:+.1f}ms" if off is not None else "NTP不可用"
    return f"🕐 当前时间\n{now}\n本机与标准时间偏差: {off_str}"


def cmd_yiyan():
    y = fetch_yiyan()
    return f"今日语录\n{y}" if y else "获取一言失败, 稍后再试"


def cmd_clean_log():
    cleaned = []
    # 清理脚本日志(截断而非删除, 避免破坏正在写入的句柄)
    for f in (LISTENER_LOG, SIGN_LOG):
        try:
            if os.path.exists(f):
                sz = os.path.getsize(f)
                open(f, "w").close()
                cleaned.append(f"{f}({sz//1024}KB)")
        except Exception as e:
            cleaned.append(f"{f}失败:{e}")
    # 清理 QQ/NapCat 日志目录
    for d in QQ_LOG_DIRS:
        if os.path.isdir(d):
            n = 0
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p):
                        os.remove(p); n += 1
                except Exception:
                    pass
            if n:
                cleaned.append(f"{d}({n}个)")
    return "🧹 日志清理完成\n" + ("\n".join(cleaned) if cleaned else "无可清理项")


def is_sign_running():
    try:
        out = subprocess.check_output(["pgrep", "-f", SIGN_SCRIPT], text=True)
        return bool(out.strip())
    except Exception:
        return False


def cmd_restart_sign():
    """重启打卡脚本。优先走 systemd(保证全局唯一进程, 不再叠加);
    systemd 不可用时回退到 pkill+后台启动(会先彻底清掉所有旧进程再起一个)。"""
    # 方式1: systemd 用户服务(推荐) —— restart 由 systemd 保证只有一个进程
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", SIGN_SERVICE],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            time.sleep(2)
            return f"🔄 打卡脚本已通过 systemd 重启: {'✅运行中' if is_sign_running() else '❌未检测到进程,请查看日志'}"
    except Exception:
        pass  # systemd 不可用, 走回退方案

    # 方式2: 回退 —— 先彻底杀光所有旧进程(避免叠加), 再后台起唯一一个
    subprocess.call(["pkill", "-f", SIGN_SCRIPT])
    time.sleep(2)
    here = os.path.dirname(os.path.abspath(__file__))          # scripts/ 目录
    with open(os.path.join(LOG_DIR, "sign_nohup.out"), "a") as out:
        subprocess.Popen(["python3", os.path.join(here, SIGN_SCRIPT)],
                         stdout=out, stderr=out,
                         cwd=os.path.dirname(here),            # 项目根(打包日志清理等相对逻辑)
                         start_new_session=True)
    time.sleep(2)
    return f"🔄 打卡脚本已重启(fallback): {'✅成功' if is_sign_running() else '❌未检测到进程,请查看日志'}"


def _do_sign(group_id):
    """对指定群真实打卡一次, 返回 (ok, 一言)。"""
    resp = api("set_group_sign", {"group_id": str(group_id)})
    ok = resp.get("retcode") == 0
    return ok, fetch_yiyan()


def cmd_test_sign():
    """每日打卡测试: 只在测试专用群 1000000003 打卡(用掉当天名额)。"""
    try:
        ok, y = _do_sign(TARGET_SIGN_GROUP)
        extra = f"\n💬 {y}" if y else ""
        return f"🧪 每日打卡测试 -> 群{TARGET_SIGN_GROUP}: {'✅已发送' if ok else '❌失败'}{extra}"
    except Exception as e:
        return f"🧪 每日打卡测试异常: {e}"


def cmd_sign_here(group_id):
    """每日打卡: 在当前群打卡(谁在哪个群@就签哪个群)。
    成功时只回干净的「📅 每日打卡 / 💬 一言」; 失败才提示原因。"""
    try:
        ok, y = _do_sign(group_id)
        if not ok:
            return f"📅 每日打卡失败, 稍后再试(群{group_id})"
        return f"📅 每日打卡\n💬 {y}" if y else "📅 每日打卡"
    except Exception as e:
        return f"📅 每日打卡异常: {e}"


# ---------------- 连续打卡天数(本地记录) ----------------
def _load_streak():
    try:
        with open(STREAK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_streak(rec):
    try:
        with open(STREAK_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except Exception as e:
        log.info(f"[连续天数保存失败] {e}")


def _bump_streak(sender_qq):
    """给某人今日打卡计数: 昨天打过则 +1, 断天则归 1, 今天已打则维持。
    返回 (连续天数, 今天是否首次)。"""
    today = time.strftime("%Y-%m-%d", time.localtime())
    yesterday = time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - 86400))
    rec = _load_streak()
    key = str(sender_qq)
    info = rec.get(key, {})
    last = info.get("last")
    days = int(info.get("days", 0))
    if last == today:
        return days, False  # 今天已经打过, 不重复计数
    if last == yesterday:
        days += 1           # 昨天打过: 连续 +1
    else:
        days = 1            # 断了(或首次): 从 1 开始
    rec[key] = {"last": today, "days": days}
    _save_streak(rec)
    return days, True


def cmd_today_sign(sender_qq, group_id):
    """今日打卡: 给当前群打卡 + 本地累加连续天数, 回显
       「今日已打卡 / 你已连续打卡xx天 / 语录」。每人每天一次(已打则无反应)。"""
    days, first = _bump_streak(sender_qq)
    if not first:
        return SILENT  # 今日已打卡过: 无反应
    try:
        _do_sign(group_id)  # 顺带真实签到(失败也不影响连续计数展示)
    except Exception:
        pass
    y = fetch_yiyan()
    lines = [f"✅ 今日已打卡", f"你已连续打卡 {days} 天"]
    if y:
        lines.append(y)
    result = "\n".join(lines)
    time.sleep(REPLY_DELAY)
    send_group(group_id, result)
    return SILENT  # 已自行发送(含延迟), dispatch 不再重复发


def cmd_menu():
    """指令菜单: 展示「所有人可用」的全部指令。
    ⚠️ 维护约定: 各服务的普通指令都集中在这里展示 ——
      listener.py 本身 + daily_image.py(每日一图, 无权限检查) +
      douyin_spark.py / spark_status.py(仅管理员, 不在本菜单出现)。
    新增普通权限指令时, 必须同步: ① 加入 COMMANDS(level="all");
      ② 加入 PUBLIC_CMDS(如需限次控制) 或直接写死在下方; ③ 更新手册指令表。"""
    lines = ["📋 指令菜单 (@我 + 关键词)", "—— 所有人可用 ——",
             "• 菜单/帮助 (本菜单, 不限次)"]
    for kw in PUBLIC_CMDS:
        tag = " (不限次)" if kw in PUBLIC_UNLIMITED else ""
        suffix = " +问题" if kw == "起卦" else ""  # 起卦需带问题正文
        lines.append(f"• {kw}{tag}{suffix}")
    # 每日一图来自 daily_image.py 服务, 无权限与次数限制
    lines.append("• 每日一图 (不限次, 随机美图)")
    lines.append("(标注「不限次」的可反复使用, 其余每人每天各一次;"
                 " 管理员请用「手册」查看全部指令)")
    return "\n".join(lines)


def cmd_menu_admin():
    """管理员手册: 列出全部指令名(含其他服务脚本的指令), 不带描述。
    ⚠️ 维护约定: 新增任何服务的 @指令 后, 必须同步更新这里 + 手册指令表。"""
    names = []
    for keys, _fn, _level, _argmode in COMMANDS:
        names.append(keys[0])  # 每条取第一个关键词作为代表
    # 跨服务指令(douyin_spark.py / spark_status.py / daily_image.py)
    names += ["添加续火花", "删除续火花", "续火花名单", "立即续火花",
              "查看火花", "每日一图"]
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return ("📖 管理员手册\n（前半为监听指令, 后半为抖音火花/每日一图）\n"
            + "\n".join(f"• {n}" for n in uniq))


def cmd_switch_on():
    global ALLOW_EVERYONE
    ALLOW_EVERYONE = True
    save_allow_everyone(True)
    return "🔓 已开启: 现在所有人都可以 @ 使用指令"


def cmd_switch_off():
    global ALLOW_EVERYONE
    ALLOW_EVERYONE = False
    save_allow_everyone(False)
    return "🔒 已关闭: 现在仅管理员可以 @ 使用指令"


# ---------------- 抽签(三才占卜) ----------------
# 三才配置吉凶表: 五行三字组合 -> 吉凶评语
_SANCAI_TABLE = """
木木木【大吉】；木木火【大吉】；木木土【大吉】；木木金【凶多吉少】；木木水【吉多于凶】
木火木【大吉】；木火火【中吉】；木火土【大吉】；木火金【凶多于吉】；木火水【大凶】
木土木【大凶】；木土火【中吉】；木土土【吉】；木土金【吉多于凶】；木土水【大凶】
木金木【大凶】；木金火【大凶】；木金土【凶多于吉】；木金金【大凶】；木金水【大凶】
木水木【大吉】；木水火【凶多于吉】；木水土【凶多于吉】；木水金【大吉】；木水水【大吉】
火木木【大吉】；火木火【大吉】；火木土【大吉】；火木金【凶多于吉】；火木水【中吉】
火火木【大吉】；火火火【中吉】；火火土【大吉】；火火金【大凶】；火火水【大凶】
火土木【吉多于凶】；火土火【大吉】；火土土【大吉】；火土金【大吉】；火土水【吉多于凶】
火金木【大凶】；火金火【大凶】；火金土【吉凶参半】；火金金【大凶】；火金水【大凶】
火水木【凶多于吉】；火水火【大凶】；火水土【大凶】；火水金【大凶】；火水水【大凶】
土木木【中吉】；土木火【中吉】；土木土【凶多于吉】；土木金【大凶】；土木水【凶多于吉】
土火木【大吉】；土火火【大吉】；土火土【大吉】；土火金【吉多于凶】；土火水【大凶】
土土木【中吉】；土土火【大吉】；土土土【大吉】；土土金【大吉】；土土水【凶多于吉】
土金木【凶多于吉】；土金火【凶多于吉】；土金土【大吉】；土金金【大吉】；土金水【大吉】
土水木【凶多于吉】；土水火【大凶】；土水土【大凶】；土水金【吉凶参半】；土水水【大凶】
金木木【凶多于吉】；金木火【凶多于吉】；金木土【凶多于吉】；金木金【大凶】；金木水【凶多于吉】
金火木【凶多于吉】；金火火【吉凶参半】；金火土【吉凶参半】；金火金【大凶】；金火水【大凶】
金土木【中吉】；金土火【大吉】；金土土【大吉】；金土金【大吉】；金土水【吉多于凶】
金金木【大凶】；金金火【大凶】；金金土【大吉】；金金金【中吉】；金金水【中吉】
金水木【大吉】；金水火【凶多于吉】；金水土【吉】；金水金【大吉】；金水水【中吉】
水木木【大吉】；水木火【大吉】；水木土【大吉】；水木金【凶多于吉】；水木水【大吉】
水火木【中吉】；水火火【大凶】；水火土【凶多于吉】；水火金【大凶】；水火水【大凶】
水土木【大凶】；水土火【中吉】；水土土【中吉】；水土金【中吉】；水土水【大凶】
水金木【凶多于吉】；水金火【凶多于吉】；水金土【大吉】；水金金【中吉】；水金水【大吉】
水水木【大吉】；水水火【大凶】；水水土【大凶】；水水金【大吉】；水水水【中吉】
"""

SANCAI_MAP = {k: v.strip("；;")
              for k, v in re.findall(r"([木火土金水]{3})【([^】]+)】", _SANCAI_TABLE)}

# 小六壬元素表(名称-五行)
_LIUREN = ["大安—木", "留连—木", "速喜—火", "赤口—金", "小吉—水",
           "空亡—土", "病符—土", "桃花—土", "天德—金"]

# 小六壬九宫「一句话精简含义」表: 用于把三宫组合成一段总结。
#   (取自用户提供的大安/留连/速喜/赤口/小吉/空亡/天德/桃花/病符 的解读, 精简为短句)
_LIUREN_MEAN = {
    "大安": "平安顺遂、光明正大, 谋事宜静、多主吉",
    "留连": "拖延反复、纠缠未明, 凡事宜缓、需防口舌",
    "速喜": "喜讯将至、进展迅速, 求财求事多有好音",
    "赤口": "口舌是非、易生破败, 凡事宜防争执与惊慌",
    "小吉": "诸事可谋、和合多利, 有人报喜、贵人相助",
    "空亡": "谋事落空、劳而无成, 求财不利、宜守不宜进",
    "天德": "贵人相助、得上司长辈之力, 求事易成",
    "桃花": "情感人缘旺、异性牵绊多, 需防感情纠葛",
    "病符": "身心易疲、事有异常, 需注意健康与阻碍",
}


def _liuren_name(e):
    """从 '大安—木' 取出宫位名 '大安'。"""
    return e.split("—")[0]


def _sancai_divine(n1, n2, n3):
    """按小六壬三宫排盘, 返回 (三个宫位名, 五行三字, 吉凶评语)。"""
    m = len(_LIUREN)
    e1 = _LIUREN[(n1 - 1) % m]
    e2 = _LIUREN[(n1 + n2 - 2) % m]
    e3 = _LIUREN[(n1 + n2 + n3 - 3) % m]
    wuxing = e1[-1] + e2[-1] + e3[-1]
    jixiong = SANCAI_MAP.get(wuxing, "未知组合")
    return (e1, e2, e3), wuxing, jixiong


def _sancai_summary(e1, e2, e3, jixiong):
    """把三宫含义组合成一段精简总结: 起因(第一宫)→发展(第二宫)→结果(第三宫)。"""
    n1, n2, n3 = _liuren_name(e1), _liuren_name(e2), _liuren_name(e3)
    m1 = _LIUREN_MEAN.get(n1, ""); m2 = _LIUREN_MEAN.get(n2, ""); m3 = _LIUREN_MEAN.get(n3, "")
    return (f"起因【{n1}】{m1}; "
            f"过程【{n2}】{m2}; "
            f"结果【{n3}】{m3}。总体: {jixiong}。")


# 抽签每日限次: {"date": "YYYY-MM-DD", "done": [qq, ...]}
def _load_chouqian():
    try:
        with open(CHOUQIAN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_chouqian(rec):
    try:
        with open(CHOUQIAN_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except Exception as e:
        log.info(f"[抽签记录保存失败] {e}")


def cmd_chouqian(sender_qq, group_id):
    """每人每天限一次的抽签: 输入=抽签人QQ + 当前年/月/日/时, 结果延迟发送。
    今日已抽过则无反应(静默)。"""
    today = time.strftime("%Y-%m-%d", time.localtime())
    rec = _load_chouqian()
    done = rec.get("done", []) if rec.get("date") == today else []
    if int(sender_qq) in [int(x) for x in done]:
        return SILENT  # 今日已抽: 无反应

    # 输入: 抽签人QQ + 当前年月日时(同一人当天多次算结果一致, 不同人当天不同)
    lt = time.localtime()
    q = int(sender_qq)
    n1 = (q % 97) + lt.tm_year
    n2 = (q // 97 % 89) + lt.tm_mon + lt.tm_hour
    n3 = (q // 9409 % 83) + lt.tm_mday + lt.tm_hour
    (e1, e2, e3), wuxing, jixiong = _sancai_divine(n1, n2, n3)
    summary = _sancai_summary(e1, e2, e3, jixiong)

    # 记录该用户今日已抽
    done.append(q)
    _save_chouqian({"date": today, "done": done})

    result = (f"🎴 [CQ:at,qq={sender_qq}] 今日一签\n"
              f"三宫: {e1} · {e2} · {e3}\n"
              f"五行: {wuxing}\n"
              f"签评: 【{jixiong}】\n"
              f"总结: {summary}\n"
              f"(每人每日一签, 明日可再抽)")

    # 抽签后延迟发送
    time.sleep(CHOUQIAN_DELAY)
    send_group(group_id, result)
    return SILENT  # 已自行发送, dispatch 不再重复发


def cmd_zhanbu(sender_qq, group_id):
    """每日不限次数的「当前占卜」: 每次随机起卦(与「抽签」不同, 抽签每人每天只一次且结果固定)。
    同样走小六壬三才排盘 + 三宫总结, 但每次随机, 可反复使用。"""
    # 随机三宫(1..元素数), 每次不同
    m = len(_LIUREN)
    n1 = random.randint(1, m)
    n2 = random.randint(1, m)
    n3 = random.randint(1, m)
    (e1, e2, e3), wuxing, jixiong = _sancai_divine(n1, n2, n3)
    summary = _sancai_summary(e1, e2, e3, jixiong)

    result = (f"🔮 [CQ:at,qq={sender_qq}] 当前占卜\n"
              f"三宫: {e1} · {e2} · {e3}\n"
              f"五行: {wuxing}\n"
              f"卦评: 【{jixiong}】\n"
              f"总结: {summary}\n"
              f"(当前占卜不限次, 可随时再卜一卦)")

    # 与其它@指令一致, 延迟后发送
    time.sleep(REPLY_DELAY)
    send_group(group_id, result)
    return SILENT  # 已自行发送, dispatch 不再重复发


def _question_to_three(question):
    """把问题文本起卦: 每个字符取 Unicode 码点(10进制), 顺次拼成一长串数字,
    再平均分成 3 大段, 每段"各位数字之和"得到 3 个正整数(用于三宫排盘)。
    返回 (n1, n2, n3, digits字符串, 三段字符串元组)。"""
    # 1) 每个字符 -> 10进制码点, 顺次拼接成一长串数字
    digits = "".join(str(ord(ch)) for ch in question)
    if not digits:
        digits = "0"
    # 2) 平均分成 3 大段(长度不整除时, 余数补到前面的段)
    n = len(digits)
    base, extra = divmod(n, 3)
    sizes = [base + (1 if i < extra else 0) for i in range(3)]
    parts, pos = [], 0
    for sz in sizes:
        parts.append(digits[pos:pos + sz])
        pos += sz
    # 3) 每段"各位数字相加"得到一个数(空段记 0)
    nums = [sum(int(c) for c in p) if p else 0 for p in parts]
    # 排盘用 (num-1)%m, 值为 0 也可安全参与; 但保证至少 1, 语义更自然
    n1, n2, n3 = (v if v > 0 else 1 for v in nums)
    return n1, n2, n3, digits, tuple(parts)


def cmd_qigua(text, sender_qq, group_id):
    """按问题起卦: 「@我 起卦+问题」或「@我 起卦 问题」。
    把问题转成 10 进制码点串 -> 分 3 段 -> 每段数字相加 -> 得 3 数起小六壬三才卦。
    不限次。"""
    # 取出「起卦」之后的问题正文(去掉可能的 + / ＋ / 冒号 / 空白前缀)
    m = re.search(r"起卦[\s+＋:：]*(.*)", text, re.S)
    question = (m.group(1) if m else "").strip()
    if not question:
        time.sleep(REPLY_DELAY)
        send_group(group_id, f"❓ [CQ:at,qq={sender_qq}] 用法: @我 起卦+你的问题  (例: 起卦+这次考试能过吗)")
        return SILENT

    n1, n2, n3, digits, parts = _question_to_three(question)
    (e1, e2, e3), wuxing, jixiong = _sancai_divine(n1, n2, n3)
    summary = _sancai_summary(e1, e2, e3, jixiong)

    result = (f"🧭 [CQ:at,qq={sender_qq}] 起卦\n"
              f"所问: {question}\n"
              f"起数: {n1} · {n2} · {n3}  (三段码点各位之和)\n"
              f"三宫: {e1} · {e2} · {e3}\n"
              f"五行: {wuxing}\n"
              f"卦评: 【{jixiong}】\n"
              f"总结: {summary}")

    time.sleep(REPLY_DELAY)
    send_group(group_id, result)
    return SILENT  # 已自行发送, dispatch 不再重复发


def cmd_add_admin(text, sender_qq):
    """超管添加管理员: 格式「添加权限+QQ号」或「添加权限 QQ号」。"""
    m = re.search(r"添加权限[+＋:：\s]*(\d{5,12})", text)
    if not m:
        return "❌ 格式: @我 添加权限+QQ号  (例: 添加权限+123456)"
    qq = int(m.group(1))
    if qq == int(SUPER_ADMIN):
        return "ℹ️ 该QQ本身就是超级管理员, 无需添加"
    if qq in ADMINS:
        return f"ℹ️ {qq} 已经是管理员了"
    ADMINS.append(qq)
    save_admins(ADMINS)
    return f"✅ 已添加管理员: {qq}\n当前管理员: {ADMINS}"


# ---------------- 打卡群列表(与 group_sign.py 共享) ----------------
def load_sign_groups():
    """读取打卡群列表; 无文件则用种子。"""
    try:
        with open(SIGN_GROUPS_FILE, "r", encoding="utf-8") as f:
            groups = [int(x) for x in json.load(f).get("groups", [])]
        if groups:
            return groups
    except Exception:
        pass
    return list(SEED_SIGN_GROUPS)


def save_sign_groups(groups):
    try:
        with open(SIGN_GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump({"groups": [int(x) for x in groups]}, f)
    except Exception as e:
        log.info(f"[打卡群保存失败] {e}")


def cmd_add_sign_group(text, sender_qq):
    """管理员添加自动打卡群:「添加每日打卡 群号」/「添加打卡群+群号」(旧别名)。
    (填的是 QQ 群号, 不是 QQ 号)"""
    m = re.search(r"(\d{5,15})", text)
    if not m:
        return "❌ 格式: @我 添加每日打卡 群号  (例: 添加每日打卡 959415637)"
    gid = int(m.group(1))
    groups = load_sign_groups()
    if gid in groups:
        return f"ℹ️ 群 {gid} 已在打卡列表里了\n当前打卡群: {groups}"
    groups.append(gid)
    save_sign_groups(groups)
    return (f"✅ 已添加每日打卡群: {gid}\n当前打卡群: {groups}\n"
            f"(下一个零点起该群自动打卡, 无需重启)")


def cmd_del_sign_group(text, sender_qq):
    """管理员删除自动打卡群:「删除每日打卡 群号」/「删除打卡群 群号」(旧别名)。"""
    m = re.search(r"(\d{5,15})", text)
    if not m:
        return "❌ 格式: @我 删除每日打卡 群号  (例: 删除每日打卡 959415637)"
    gid = int(m.group(1))
    groups = load_sign_groups()
    if gid not in groups:
        return f"ℹ️ 群 {gid} 不在打卡列表中\n当前打卡群: {groups}"
    groups.remove(gid)
    save_sign_groups(groups)
    return (f"🗑 已删除每日打卡群: {gid}\n剩余打卡群: {groups}\n"
            f"(下一个零点起不再为该群自动打卡)")


# ---------------- 普通人「每种指令每天各一次」限次 ----------------
def _public_cmd_key(text):
    """把用户文本归一到白名单里的一个指令key(取第一个命中的)。"""
    for kw in PUBLIC_CMDS:
        if kw in text:
            return kw
    return None


def public_limit_check_and_mark(sender_qq, cmd_key):
    """普通人限次: 返回 True=可用(并记为已用), False=今天该指令已用过。
    记录结构: {"date": "YYYY-MM-DD", "used": {"QQ": ["抽签", "一言"]}}"""
    today = time.strftime("%Y-%m-%d", time.localtime())
    try:
        with open(PUBLIC_LIMIT_FILE, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        rec = {}
    if rec.get("date") != today:
        rec = {"date": today, "used": {}}
    used = rec.setdefault("used", {})
    mine = used.setdefault(str(sender_qq), [])
    if cmd_key in mine:
        return False
    mine.append(cmd_key)
    try:
        with open(PUBLIC_LIMIT_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except Exception as e:
        log.info(f"[普通人限次保存失败] {e}")
    return True


# ---------------- 单群@开关(持久化) ----------------
def load_off_groups():
    """返回被关闭@功能的群号集合。"""
    try:
        with open(GROUP_SWITCH_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f).get("off", [])}
    except Exception:
        return set()


def save_off_groups(off):
    try:
        with open(GROUP_SWITCH_FILE, "w", encoding="utf-8") as f:
            json.dump({"off": sorted(int(x) for x in off)}, f)
    except Exception as e:
        log.info(f"[单群开关保存失败] {e}")


def set_group_switch(group_id, on):
    """on=True 开启该群@功能, on=False 关闭。返回操作说明。"""
    off = load_off_groups()
    gid = int(group_id)
    if on:
        off.discard(gid)
        save_off_groups(off)
        return f"🔓 已开启群 {gid} 的@指令功能(所有人可用)"
    else:
        off.add(gid)
        save_off_groups(off)
        return f"🔒 已关闭群 {gid} 的@指令功能(仅管理员可用, 普通人被忽略)"


def cmd_group_switch(text, sender_qq):
    """管理员单群开关。格式:「群号 状态：开启/关闭」或「群号 关闭」。
    兼容: 提取文本里的群号 + 「开启/关闭」关键词。"""
    m = re.search(r"(\d{5,15})", text)
    if not m:
        return "❌ 格式: @我 群号 状态：开启/关闭  (例: 1000000003 状态：关闭)"
    gid = int(m.group(1))
    if "开启" in text or "打开" in text or "开机" in text:
        on = True
    elif "关闭" in text or "关掉" in text:
        on = False
    else:
        return "❌ 未识别开/关。格式: @我 群号 状态：开启/关闭"
    return set_group_switch(gid, on)


# ---------------- @冷却(防刷屏) ----------------
# 规则:
#   • 10 秒(AT_WINDOW_SEC)窗口内, 同一人@机器人超过 2 次(AT_MAX_IN_WIN)->该人冷却 120 分钟;
#   • 同一窗口内有 >=2 个不同的人各自都超限 ->整群冷却 120 分钟;
#   • 冷却触发时"只提示一次", 且对同一人/同一群"永久只提示一次"(notified 持久化)。
# 冷却期内该人/该群的@一律静默忽略(不回复任何内容)。
_at_history = {}   # {(group_id, qq): [ts, ...]}  内存滑动窗口, 进程内即可


def _load_cooldown():
    try:
        with open(COOLDOWN_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("users", {})            # {"gid:qq": 解冻unix时间}
    d.setdefault("groups", {})           # {"gid": 解冻unix时间}
    d.setdefault("notified_users", [])   # ["gid:qq", ...] 永久已提示过
    d.setdefault("notified_groups", [])  # ["gid", ...]    永久已提示过
    return d


def _save_cooldown(d):
    try:
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception as e:
        log.info(f"[冷却状态保存失败] {e}")


def _record_at_and_check(group_id, qq):
    """记录一次@并做冷却判定。返回 (action, notice):
       action = "ok"     正常放行
              = "silent" 冷却期内, 静默(不回复)
              = "notify" 刚触发冷却, 需发送 notice(仅此一次)
       notice = 需要发送的提示文本(仅 action=='notify' 时非空)。"""
    now = time.time()
    gkey = str(group_id)
    ukey = f"{group_id}:{qq}"
    d = _load_cooldown()

    # 1) 群级冷却优先
    g_until = float(d["groups"].get(gkey, 0))
    if now < g_until:
        return ("silent", "")
    # 2) 个人冷却
    u_until = float(d["users"].get(ukey, 0))
    if now < u_until:
        return ("silent", "")

    # 3) 记录本次@并裁剪窗口
    hist = _at_history.setdefault((group_id, qq), [])
    hist.append(now)
    cutoff = now - AT_WINDOW_SEC
    hist[:] = [t for t in hist if t >= cutoff]

    over = len(hist) > AT_MAX_IN_WIN  # 本人是否在窗口内超限
    if not over:
        return ("ok", "")

    # 4) 本人超限 -> 先看是否升级为"集体冷却"
    #    统计本群窗口内有多少不同的人也超限
    over_users = 0
    for (g, u), ts in _at_history.items():
        if g != group_id:
            continue
        ts[:] = [t for t in ts if t >= cutoff]
        if len(ts) > AT_MAX_IN_WIN:
            over_users += 1
    if over_users >= GROUP_COOLDOWN_MIN_USERS:
        # 集体冷却
        d["groups"][gkey] = now + COOLDOWN_SEC
        already = gkey in d["notified_groups"]
        if not already:
            d["notified_groups"].append(gkey)
        _save_cooldown(d)
        if already:
            return ("silent", "")
        return ("notify",
                f"⚠️ 检测到多人连续@刷屏, 本群@功能已进入冷却"
                f"{COOLDOWN_SEC//60}分钟, 期间不再响应(本提示仅此一次)。")

    # 5) 仅本人冷却
    d["users"][ukey] = now + COOLDOWN_SEC
    already = ukey in d["notified_users"]
    if not already:
        d["notified_users"].append(ukey)
    _save_cooldown(d)
    if already:
        return ("silent", "")
    return ("notify",
            f"⚠️ [CQ:at,qq={qq}] 你连续@太频繁, 已进入冷却"
            f"{COOLDOWN_SEC//60}分钟, 期间不再响应(本提示仅此一次)。")


TARGET_SIGN_GROUP = 1000000003  # 「每日打卡测试」目标群(测试专用, 与 group_sign 一致)

# 静默哨兵: dispatch 返回它表示"命中指令但权限不足", handle_event 不发任何消息。
# (区别于返回 None = 完全没匹配到指令, 会回复"未识别")
SILENT = object()

# 权限级别:
#   "all"      = 公开指令; 普通人可用(受白名单 PUBLIC_CMDS 约束 + 每天各一次限次), 管理员无限制
#   "admin"    = 始终仅管理员(超管/ADMINS)可用; 普通人静默(不暴露其存在)
#   "super"    = 始终仅超级管理员可用
# 参数模式(第5字段, 决定如何调用处理函数):
#   "none"    = fn()
#   "gid"     = fn(group_id)
#   "qq_gid"  = fn(sender_qq, group_id)   —— 需知道谁抽/发到哪个群(抽签)
#   "text_qq" = fn(text, sender_qq)       —— 需解析原文+谁发的(添加权限)
#   "text_qq_gid" = fn(text, sender_qq, group_id) —— 需原文+谁发+发到哪个群(起卦+问题)
# 指令路由: (关键词元组, 处理函数, 权限级别, 参数模式)
#   ⚠️ 关键词有包含关系时, 更具体的要排在前面:
#      "每日打卡测试" 必须在 "每日打卡" 之前, 否则会被后者先命中。
#
# 说明: 普通人「可见/可用」的公开指令由上面的 PUBLIC_CMDS 白名单决定(抽签/一言等,
#   可后续扩展)。凡不在白名单里的指令, 普通人一律静默、当作不存在; 管理员可用全部。
COMMANDS = [
    (("手册", "指令手册", "管理手册"), cmd_menu_admin, "admin",    "none"),
    (("菜单", "帮助", "help", "?"), cmd_menu,        "all",      "none"),
    (("状态", "status"),            cmd_status,      "admin",    "none"),
    (("时间", "time"),              cmd_time,        "admin",    "none"),
    (("一言",),                     cmd_yiyan,       "all",      "none"),
    (("起卦",),                     cmd_qigua,       "all",      "text_qq_gid"),
    (("当前占卜", "占卜"),           cmd_zhanbu,      "all",      "qq_gid"),
    (("抽签", "求签"),               cmd_chouqian,    "all",      "qq_gid"),
    (("今日打卡", "连续打卡", "打卡签到"), cmd_today_sign, "all",   "qq_gid"),
    (("添加权限", "加权限", "添加管理"), cmd_add_admin, "super",   "text_qq"),
    # 注意: 「添加/删除每日打卡」必须排在「每日打卡」之前, 避免被后者先命中
    (("添加每日打卡", "添加打卡群", "加打卡群", "新增每日打卡"),
                                    cmd_add_sign_group, "admin", "text_qq"),
    (("删除每日打卡", "删除打卡群", "移除打卡群", "取消每日打卡"),
                                    cmd_del_sign_group, "admin", "text_qq"),
    (("每日打卡测试", "打卡测试", "测试打卡", "测试签到"), cmd_test_sign, "admin", "none"),
    (("每日打卡", "本群打卡"),       cmd_sign_here,   "admin",    "gid"),
    (("清理日志", "清日志"),         cmd_clean_log,   "admin",    "none"),
    (("重启打卡", "重启签到"),       cmd_restart_sign, "admin",   "none"),
    (("开启所有人", "开启全员", "@开启"), cmd_switch_on,  "super",  "none"),
    (("关闭所有人", "关闭全员", "@关闭"), cmd_switch_off, "super",  "none"),
]


def dispatch(text, sender_qq, group_id):
    privileged = is_privileged(sender_qq)

    # ── 优先: 单群@开关(管理员及以上)。格式: @我 群号 状态：开启/关闭。
    #    放最前面, 避免被 "状态"(cmd_status) 或群号误命中其它指令。
    if ("状态" in text and re.search(r"\d{5,15}", text)
            and ("开启" in text or "关闭" in text or "打开" in text or "关掉" in text)):
        if not privileged:
            return SILENT  # 普通人不可操作开关, 静默
        return cmd_group_switch(text, sender_qq)

    for keys, fn, level, argmode in COMMANDS:
        if not any(k in text for k in keys):
            continue

        # ── 超管专属
        if level == "super":
            if int(sender_qq) != int(SUPER_ADMIN):
                return SILENT
        # ── 管理员专属(含普通人不可见的所有指令)
        elif level == "admin":
            if not privileged:
                return SILENT  # 普通人: 静默, 当作不存在
        # ── 公开指令("all"): 管理员无限制; 普通人受白名单+每日限次约束
        else:
            if not privileged:
                # 「菜单/帮助」放行且不限次(让普通人能看到可用指令)
                if fn is not cmd_menu:
                    cmd_key = _public_cmd_key(text)
                    if cmd_key is None:
                        return SILENT  # 命中的不是白名单公开指令: 静默
                    # 「不限次」的公开指令(如占卜)跳过每日限次, 可反复使用
                    if cmd_key not in PUBLIC_UNLIMITED:
                        if not public_limit_check_and_mark(sender_qq, cmd_key):
                            return SILENT  # 今天该指令已用过: 静默

        if argmode == "gid":
            return fn(group_id)
        elif argmode == "qq_gid":
            return fn(sender_qq, group_id)
        elif argmode == "text_qq":
            return fn(text, sender_qq)
        elif argmode == "text_qq_gid":
            return fn(text, sender_qq, group_id)
        else:  # "none"
            return fn()
    return None  # 未匹配到指令


# ---------------- 消息解析 ----------------
def extract_text_and_at(msg_field):
    """从 array 格式的 message 里取纯文本, 并判断是否 @ 了机器人。"""
    text_parts, at_self = [], False
    if isinstance(msg_field, list):
        for seg in msg_field:
            t = seg.get("type"); d = seg.get("data", {})
            if t == "text":
                text_parts.append(d.get("text", ""))
            elif t == "at":
                if str(d.get("qq")) == str(BOT_QQ):
                    at_self = True
    elif isinstance(msg_field, str):
        text_parts.append(msg_field)
        at_self = f"[CQ:at,qq={BOT_QQ}]" in msg_field
    return "".join(text_parts).strip(), at_self


def handle_event(ev):
    if ev.get("post_type") != "message" or ev.get("message_type") != "group":
        return
    group_id = ev.get("group_id")
    if GROUPS_ALLOW and group_id not in GROUPS_ALLOW:
        return
    user_id = ev.get("user_id")
    text, at_self = extract_text_and_at(ev.get("message"))
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log.info(f"[{ts}] 群 {group_id} 用户 {user_id}: at_self={at_self} text={text}")

    if at_self:
        privileged = is_privileged(user_id)

        # 单群@开关: 该群被「关闭」时, 普通人@一律静默忽略; 管理员/超管仍可用。
        if group_id in load_off_groups() and not privileged:
            log.info(f"[{ts}] 群 {group_id} 已关闭@功能, 忽略普通用户 {user_id}")
            return

        # @冷却(防刷屏): 管理员/超管不受冷却限制。
        if not privileged:
            action, notice = _record_at_and_check(group_id, user_id)
            if action == "silent":
                log.info(f"[{ts}] 群 {group_id} 用户 {user_id} 冷却中, 静默")
                return
            if action == "notify":
                time.sleep(REPLY_DELAY)
                send_group(group_id, notice)
                log.info(f"[{ts}] 群 {group_id} 触发冷却提示(仅一次): {notice[:20]}")
                return

        reply = dispatch(text, user_id, group_id)
        if reply is SILENT:
            return  # 权限不足/超限/非白名单/已自行发送: 不再发任何消息
        if reply is None:
            # 未识别指令: 静默不提示(由 gemini_qq_bridge 的本地 AI 聊天兜底,
            # @机器人 + 任意内容 -> AI 回复, 不再出现「未识别」提示)
            return
        # 所有@指令回复统一延迟 1 秒再发送(自行发送的指令已在内部延迟并返回 SILENT)
        time.sleep(REPLY_DELAY)
        send_group(group_id, reply)
        log.info(f"[{ts}] -> 回复: {reply.splitlines()[0]}...")


# ---------------- 主循环(断线重连) ----------------
def main():
    log.info("=" * 50)
    log.info(f"listener 启动 | bot={BOT_QQ} ws={WS_HOST}:{WS_PORT}")
    while True:
        ws = WSClient(WS_HOST, WS_PORT, "/", WS_TOKEN)
        try:
            ws.connect()
            log.info("[WS] 已连接 NapCat")
            while True:
                raw = ws.recv()
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                try:
                    handle_event(ev)
                except Exception as e:
                    log.info(f"[处理异常] {e}")
        except Exception as e:
            log.info(f"[WS] 连接断开: {e}, 5秒后重连")
            ws.close()
            time.sleep(5)


if __name__ == "__main__":
    main()
