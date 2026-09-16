#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
group_sign.py — QQ 群「零点抢第一」自动打卡脚本

功能:
  1. 每天 00:00:00 (NTP 校时) 精确调用群签到 API (set_group_sign) 抢第一
  2. 打卡后调用「一言」API 拿一句话, 发送到群里
  3. 为最大化抢第一概率: NTP 网络校时 + 连接预热 + 毫秒级忙等 + 网络延迟提前补偿

依赖: requests (已安装)。NTP 用原生 socket 实现, 无需额外库。

⚠️ 说明: 脚本已把工程能做的都做到极致, 但没有任何脚本能"100%保证"第一名——
   最终还受网络抖动、对手、群机器人风控影响。这里做的是"最大化概率"。
"""

import socket
import struct
import time
import re
import os
import sys
import json
import fcntl
import logging
import requests
import resource

# ============ 内存限制 ============
MEM_LIMIT_MB = 64
try:
    resource.setrlimit(resource.RLIMIT_AS,
                      (MEM_LIMIT_MB * 1024 * 1024, MEM_LIMIT_MB * 1024 * 1024))
except Exception:
    pass

# ============ 目录结构(脚本在 scripts/, 配置在 conf/, 日志在 logs/) ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))    # scripts/ 目录
ROOT_DIR = os.path.dirname(BASE_DIR)                     # 项目根 NapCatQQ-dokcer/
CONF_DIR = os.path.join(ROOT_DIR, "conf")                # 配置/状态 json
LOG_DIR  = os.path.join(ROOT_DIR, "logs")                # 日志目录

# ============ 配置区 (按需修改) ============
NAPCAT_HTTP   = "http://127.0.0.1:3000"   # NapCat HTTP API 地址
NAPCAT_TOKEN  = "napcat"                    # HTTP API token
BOT_QQ        = 1000000001                  # 打卡用的账号
# 打卡群列表: 持久化在 conf/sign_groups.json, listener 的「添加打卡群」指令会写入它。
# 下面是"种子默认值": 首次运行(无文件)时用它并写入文件; 之后以文件为准, 零点每轮重新读取。
SEED_GROUP_IDS = [111111111, 222222222, 333333333]   # 目标群号(可多个, 都会在零点打卡)
SIGN_GROUPS_FILE = os.path.join(CONF_DIR, "sign_groups.json")
GROUP_ID      = SEED_GROUP_IDS[0]           # 兼容旧引用; 测试(--test)只用这个群

YIYAN_API     = "https://v.api.aa1.cn/api/yiyan/index.php"  # 一言 API
SEND_YIYAN    = True                        # 打卡成功后是否发一言到群里

# 抢第一相关参数
NTP_SERVERS   = ["ntp.aliyun.com", "cn.ntp.org.cn", "ntp.tencent.com"]
FIRE_HOUR     = 0                           # 打卡时刻: 时
FIRE_MINUTE   = 0                           # 打卡时刻: 分
FIRE_SECOND   = 0                           # 打卡时刻: 秒
LEAD_MS       = 35                          # 提前多少毫秒发出请求, 用于补偿"NapCat->腾讯服务器"上行延迟
                                            #   实测: 脚本->NapCat 本地仅 ~1ms; NapCat->腾讯上行 ~30ms
                                            #   合计约 35ms。若打卡被判"未到点"请调小(如20); 总差一点被抢先则调大
                                            #   ⚠️ 真实打卡后看 sign.log 里的"本地RTT"再精调
SPIN_MS       = 60                          # 最后多少毫秒进入忙等自旋(高精度), 其余时间用 sleep
RESYNC_BEFORE = 120                         # 触发前多少秒重新 NTP 校时一次

LOG_FILE      = os.path.join(LOG_DIR, "sign.log")
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("group_sign")


# ---------- 打卡群列表(持久化, 与 listener 共享) ----------
def load_sign_groups():
    """读取打卡群列表。首次(无文件)用种子写入; 文件损坏则回退种子。
    每次零点打卡前都会调用, 因此 listener 新增的群下一个零点即生效, 无需重启。"""
    try:
        with open(SIGN_GROUPS_FILE, "r", encoding="utf-8") as f:
            groups = [int(x) for x in json.load(f).get("groups", [])]
        if groups:
            return groups
    except FileNotFoundError:
        pass
    except Exception as e:
        log.info(f"读取打卡群文件失败, 用默认种子: {e}")
    # 无文件或为空: 用种子并落盘
    save_sign_groups(SEED_GROUP_IDS)
    return list(SEED_GROUP_IDS)


def save_sign_groups(groups):
    try:
        with open(SIGN_GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump({"groups": [int(x) for x in groups]}, f)
    except Exception as e:
        log.info(f"写打卡群文件失败: {e}")


# ---------- NTP 校时 ----------
def ntp_offset(servers=NTP_SERVERS, timeout=5):
    """返回 (offset, server)。offset = 标准时间 - 本地时间(秒)。取多台取中位数更稳。"""
    NTP_DELTA = 2208988800  # 1900 -> 1970
    offsets = []
    for host in servers:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            pkt = b"\x1b" + 47 * b"\0"
            t0 = time.time()
            s.sendto(pkt, (host, 123))
            data, _ = s.recvfrom(48)
            t3 = time.time()
            s.close()
            if len(data) < 48:
                continue
            # 服务器接收(t1)与发送(t2)时间戳: 字段8和10
            recv_i, recv_f = struct.unpack("!II", data[32:40])
            tx_i,   tx_f   = struct.unpack("!II", data[40:48])
            t1 = (recv_i - NTP_DELTA) + recv_f / 2**32
            t2 = (tx_i   - NTP_DELTA) + tx_f / 2**32
            # NTP 标准 offset 公式
            off = ((t1 - t0) + (t2 - t3)) / 2
            offsets.append((off, host))
        except Exception as e:
            log.info(f"NTP {host} 失败: {e}")
    if not offsets:
        raise RuntimeError("所有 NTP 服务器都不可用")
    offsets.sort(key=lambda x: x[0])
    mid = offsets[len(offsets) // 2]
    log.info(f"NTP 校时完成 offset={mid[0]*1000:.1f}ms via {mid[1]} "
             f"(样本 {[f'{o*1000:.1f}ms' for o,_ in offsets]})")
    return mid[0], mid[1]


def now_true(offset):
    """校正后的真实 unix 时间。"""
    return time.time() + offset


# ---------- NapCat API ----------
_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {NAPCAT_TOKEN}",
    "Content-Type": "application/json",
    "Connection": "keep-alive",
})


def prewarm():
    """预热连接: 提前建立 TCP+HTTP keep-alive, 消除首包握手延迟。"""
    try:
        _session.get(f"{NAPCAT_HTTP}/get_status", timeout=5)
    except Exception as e:
        log.info(f"预热失败(忽略): {e}")


def set_group_sign(group_id=GROUP_ID):
    """调用群签到 API。"""
    r = _session.post(f"{NAPCAT_HTTP}/set_group_sign",
                      json={"group_id": str(group_id)}, timeout=10)
    return r.json()


def send_group_msg(group_id, text):
    r = _session.post(f"{NAPCAT_HTTP}/send_group_msg",
                      json={"group_id": int(group_id), "message": text}, timeout=10)
    return r.json()


def fetch_yiyan():
    """一言 API 返回的是 <p>...</p> 形式的 HTML, 需要清洗。"""
    try:
        r = requests.get(YIYAN_API, timeout=8)
        text = r.text.strip()
        text = re.sub(r"<[^>]+>", "", text).strip()  # 去掉 html 标签
        return text or None
    except Exception as e:
        log.info(f"获取一言失败: {e}")
        return None


# ---------- 精确定时 ----------
def next_fire_epoch(offset):
    """计算下一个触发时刻的真实 unix 时间 (本地时区的 00:00:00)。"""
    t = now_true(offset)
    lt = time.localtime(t)
    # 今天的目标时刻
    target = time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday,
                               FIRE_HOUR, FIRE_MINUTE, FIRE_SECOND,
                               0, 0, -1))
    target_epoch = time.mktime(target)
    if target_epoch <= t:
        target_epoch += 86400  # 已过今天则等明天
    return target_epoch


def wait_until(target_epoch, offset):
    """先粗睡, 最后 SPIN_MS 毫秒忙等自旋, 命中 (target - LEAD_MS)。"""
    fire_at = target_epoch - LEAD_MS / 1000.0
    spin_s = SPIN_MS / 1000.0
    while True:
        remain = fire_at - now_true(offset)
        if remain <= 0:
            return
        if remain > spin_s:
            time.sleep(min(remain - spin_s, 1.0))  # 分段睡, 便于响应
        else:
            # 忙等自旋阶段
            while fire_at - now_true(offset) > 0:
                pass
            return


# ---------- 主流程 ----------
def do_one_round():
    offset, _ = ntp_offset()
    target = next_fire_epoch(offset)
    tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(target))
    log.info(f"下次打卡目标时刻: {tstr}  (提前 {LEAD_MS}ms 发出, 自旋 {SPIN_MS}ms)")

    # 触发前 RESYNC_BEFORE 秒: 粗睡到临近点, 再校时一次并预热
    presync_at = target - RESYNC_BEFORE
    while now_true(offset) < presync_at:
        time.sleep(min(presync_at - now_true(offset), 5.0))

    log.info("临近打卡, 重新 NTP 校时 + 预热连接...")
    try:
        offset, _ = ntp_offset()
    except Exception as e:
        log.info(f"二次校时失败, 沿用旧 offset: {e}")
    target = next_fire_epoch(offset) if now_true(offset) >= target else target
    prewarm()
    # 读取本轮打卡群列表(每轮重新读, listener 新增的群下一个零点即生效)
    group_ids = load_sign_groups()

    # 精确等待并发射(到点后对所有群依次打卡, 抢第一以第一个群为主)
    wait_until(target, offset)
    t_send = now_true(offset)
    actual = time.strftime("%H:%M:%S", time.localtime(t_send))
    ms = int((t_send % 1) * 1000)
    log.info(f"⏱ 到点发射 @ {actual}.{ms:03d}  LEAD_MS={LEAD_MS}  目标群={group_ids}")

    for gid in group_ids:
        t_perf = time.perf_counter()
        try:
            resp = set_group_sign(gid)
            rtt_ms = (time.perf_counter() - t_perf) * 1000
            log.info(f"  ✅ 群{gid} 打卡已发送  本地RTT={rtt_ms:.1f}ms  resp={resp}")
        except Exception as e:
            log.info(f"  ❌ 群{gid} 打卡异常: {e}")
            continue
        # 发一言
        if SEND_YIYAN:
            y = fetch_yiyan()
            if y:
                try:
                    r = send_group_msg(gid, f"📅 每日打卡\n💬 {y}")
                    log.info(f"     群{gid} 已发一言: {y}")
                except Exception as e:
                    log.info(f"     群{gid} 发一言失败: {e}")
    log.info(f"   [精调提示] 若被判未到点->调小LEAD_MS; 若被抢先->调大。当前提前 {LEAD_MS}ms")


def measure_latency(n=30):
    """实测脚本->NapCat 本地 RTT, 给出 LEAD_MS 建议。"""
    import statistics
    prewarm()
    lat = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            _session.get(f"{NAPCAT_HTTP}/get_status", timeout=5)
        except Exception as e:
            log.info(f"测量出错: {e}"); continue
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    med = statistics.median(lat)
    log.info(f"本地 RTT: min={min(lat):.2f} 中位={med:.2f} "
             f"p90={lat[int(len(lat)*0.9)-1]:.2f} max={max(lat):.2f} ms (n={len(lat)})")
    # 本地约1ms可忽略, 主要补偿 NapCat->腾讯上行(~30ms)。给出总建议值。
    suggest = round(med + 30)
    log.info(f"建议 LEAD_MS ≈ {suggest} (本地中位{med:.1f}ms + 腾讯上行约30ms)。"
             f"真实打卡后按 sign.log 再精调。")


# ---------- 单实例锁 ----------
# 防止多个 group_sign 常驻进程同时运行导致"零点重复打卡/重复发一言"。
# 用 fcntl.flock 抢占锁文件: 抢到才继续, 抢不到说明已有实例在跑, 直接退出。
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".group_sign.lock")
_lock_fp = None  # 保持文件句柄存活, 进程退出时锁自动释放


def acquire_single_instance_lock():
    """抢单实例锁。成功返回 True; 已有实例在跑则返回 False。"""
    global _lock_fp
    try:
        _lock_fp = open(LOCK_FILE, "w")
        fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
        return True
    except (BlockingIOError, OSError):
        return False


def main():
    if "--measure" in sys.argv:
        log.info("=== 延迟测量模式 ===")
        measure_latency()
        return
    if "--test" in sys.argv:
        # 测试模式: 忽略零点, 10秒后立即真实打卡一次, 用于验证全链路
        log.info("=== 测试模式: 10秒后执行一次真实打卡 ===")
        offset, _ = ntp_offset()
        prewarm()
        target = now_true(offset) + 10
        wait_until(target, offset)
        t_perf = time.perf_counter()
        resp = set_group_sign(GROUP_ID)
        log.info(f"测试打卡 resp={resp}  本地RTT={(time.perf_counter()-t_perf)*1000:.1f}ms")
        if SEND_YIYAN:
            y = fetch_yiyan()
            if y:
                msg = f"📅 每日打卡\n💬 {y}"
                log.info(f"一言: {y}  send_resp={send_group_msg(GROUP_ID, msg)}")
        return

    # 常驻模式: 先抢单实例锁, 防止多进程同时零点打卡(重复发一言)
    if not acquire_single_instance_lock():
        log.info("⚠️ 已有 group_sign 实例在运行, 本进程退出(避免重复打卡)")
        return

    log.info("=" * 50)
    log.info(f"group_sign 启动 | bot={BOT_QQ} groups={load_sign_groups()} "
             f"目标 {FIRE_HOUR:02d}:{FIRE_MINUTE:02d}:{FIRE_SECOND:02d}")
    while True:
        try:
            do_one_round()
        except Exception as e:
            log.info(f"本轮异常, 60s 后重试: {e}")
            time.sleep(60)
        # 打卡完等 5s 避免同一秒重复触发, 再进入下一天循环
        time.sleep(5)


if __name__ == "__main__":
    main()
