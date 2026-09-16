#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_template.py — 新功能开发模板

这是一个「独立可运行」的示例, 演示如何用纯标准库调用 NapCat 的 HTTP API
实现你自己的功能。你有两种扩展方式:

  方式A(推荐, 简单): 在 listener.py 的 COMMANDS 里加一条指令
  方式B: 照本模板写一个独立脚本(定时任务/一次性操作等)

────────────────────────────────────────────────────────
【方式A】给 @机器人 加一个新指令 (改 listener.py)
────────────────────────────────────────────────────────
1) 在 listener.py 里写一个处理函数, 返回要回复的字符串:

       def cmd_tianqi():
           # 例: 调用某天气API, 返回文本
           return "☀️ 今天晴, 25℃"

2) 把它注册到 COMMANDS 列表(关键词, 函数, 是否需要管理员):

       COMMANDS = [
           ...
           (("天气", "weather"), cmd_tianqi, False),
       ]

3) 重启 listener 即可: 群里发「@机器人 天气」就会触发。

────────────────────────────────────────────────────────
【方式B】独立脚本 (照抄下面的骨架)
────────────────────────────────────────────────────────
下面的 main() 演示了最常用的几个操作。直接 `python3 plugin_template.py` 运行。
"""

import json
import re
import time
import urllib.request

# ============ 配置(与其它脚本保持一致) ============
HTTP_BASE  = "http://127.0.0.1:3000"
HTTP_TOKEN = "napcat"
GROUP_ID   = 111111111
# ==================================================


# ---------- 通用 API 调用封装 (POST) ----------
def api(action, payload=None):
    """调用 NapCat OneBot11 HTTP API。action 如 'send_group_msg'。"""
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"{HTTP_BASE}/{action}",
        data=data,
        headers={"Authorization": f"Bearer {HTTP_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


# ---------- 常用操作示例 ----------
def send_group_text(group_id, text):
    """发送纯文本到群。"""
    return api("send_group_msg", {"group_id": int(group_id), "message": text})


def send_group_at(group_id, qq, text):
    """@某人 + 文本 (array 消息格式)。"""
    msg = [
        {"type": "at", "data": {"qq": str(qq)}},
        {"type": "text", "data": {"text": " " + text}},
    ]
    return api("send_group_msg", {"group_id": int(group_id), "message": msg})


def send_group_image(group_id, url):
    """发送图片(URL 或本地 file:// 路径)。"""
    msg = [{"type": "image", "data": {"file": url}}]
    return api("send_group_msg", {"group_id": int(group_id), "message": msg})


def group_sign(group_id):
    """群打卡/签到。"""
    return api("set_group_sign", {"group_id": str(group_id)})


def get_group_members(group_id):
    """获取群成员列表。"""
    return api("get_group_member_list", {"group_id": int(group_id)})


# ---------- 调用外部 HTTP API 的示例(GET) ----------
def http_get(url, strip_html=True):
    """GET 一个外部接口, 返回文本。strip_html=True 时去掉 <标签>。"""
    with urllib.request.urlopen(url, timeout=8) as r:
        text = r.read().decode("utf-8", "replace").strip()
    return re.sub(r"<[^>]+>", "", text).strip() if strip_html else text


# ============ 你的功能写在这里 ============
def main():
    # 示例1: 发一条文本
    print("发送文本:", send_group_text(GROUP_ID, "🔧 这是来自模板的测试消息"))

    # 示例2: 调用外部一言API并转发
    yiyan = http_get("https://v.api.aa1.cn/api/yiyan/index.php")
    print("一言:", yiyan)
    # send_group_text(GROUP_ID, f"💬 {yiyan}")   # 取消注释即真正发送

    # 示例3: @某人 (把 10001 换成真实QQ)
    # print(send_group_at(GROUP_ID, 10001, "你被点名了"))


if __name__ == "__main__":
    main()
