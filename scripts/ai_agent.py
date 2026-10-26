#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_agent.py — AI Agent 模式(默认关闭)

开启后, 模型不再只能一次性回答, 而是可以多步调用工具:
  search  联网搜索      {"tool":"search","query":"..."}
  fetch   读取网页正文  {"tool":"fetch","url":"https://..."}
  time    当前时间      {"tool":"time"}

工作方式(纯提示词协议, 不依赖模型的 function calling 能力):
  1. 在系统提示里告诉模型工具用法与 JSON 输出格式;
  2. 模型输出一行 JSON = 请求调工具 -> 执行 -> 把【工具结果】追加回对话;
  3. 模型输出普通文本 = 最终回答, 结束; 最多 agent_max_steps 步。

配置(conf/ai_config.json):
  "agent_enabled": false
  "agent_max_steps": 3
"""

import json
import re
import time

import web_tool

_TOOL_PROMPT = (
    "\n\n【Agent 模式】你可以调用工具, 最多 {steps} 步。可用工具:\n"
    "- 联网搜索: {{\"tool\":\"search\",\"query\":\"要搜的关键词\"}}\n"
    "- 读取网页: {{\"tool\":\"fetch\",\"url\":\"https://...\"}}\n"
    "- 当前时间: {{\"tool\":\"time\"}}\n"
    "需要工具时, 只输出一行 JSON(不要解释、不要代码块); "
    "拿到【工具结果】后判断: 还需要就再输出 JSON, 不需要就直接用中文正常回答。"
    "最终回答里不要再出现 JSON。\n"
)

_JSON_RE = re.compile(r"\{[^{}]*\"tool\"[^{}]*\}", re.S)


def wants_agent(cfg):
    return bool((cfg or {}).get("agent_enabled", False))


def build_prompt(system_prompt, cfg):
    steps = int((cfg or {}).get("agent_max_steps", 3))
    return system_prompt + _TOOL_PROMPT.format(steps=steps)


def parse_action(text):
    """从模型输出里提取工具调用; 不是工具调用则返回 None。"""
    t = (text or "").strip()
    if not t or "tool" not in t:
        return None
    m = _JSON_RE.search(t)
    if not m:
        return None
    try:
        action = json.loads(m.group(0))
    except Exception:
        return None
    return action if isinstance(action, dict) and action.get("tool") else None


def execute(action, cfg):
    """执行一个工具调用, 返回给模型看的文本结果。"""
    tool = str(action.get("tool") or "").strip().lower()
    try:
        if tool == "search":
            q = str(action.get("query") or action.get("q") or "").strip()
            if not q:
                return "工具错误: search 需要 query"
            r = web_tool.search(q, cfg)
            return r or "搜索无结果(或联网功能已关闭)"
        if tool == "fetch":
            url = str(action.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return "工具错误: fetch 需要 http(s) 链接"
            title, body = web_tool.fetch_page(
                url, float((cfg or {}).get("web_timeout", 8)))
            return f"标题: {title}\n正文摘录:\n{body}"
        if tool == "time":
            return time.strftime("%Y-%m-%d %H:%M:%S %A", time.localtime())
        return f"工具错误: 未知工具 {tool}"
    except Exception as e:
        return f"工具执行失败: {e}"


def run(chat, messages, cfg, log=None):
    """Agent 循环; chat(messages) 返回模型文本。返回最终回答文本。"""
    steps = max(1, int((cfg or {}).get("agent_max_steps", 3)))
    for _ in range(steps):
        answer = chat(messages)
        action = parse_action(answer)
        if not action:
            return answer
        if log:
            log(f"[Agent] 调用工具: {action}")
        obs = execute(action, cfg)
        if log:
            log(f"[Agent] 工具结果({len(obs)}字)")
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": "【工具结果】\n" + obs})
    messages.append({"role": "user",
                     "content": "【系统】工具调用已达上限，请直接给出最终回答，不要再输出 JSON。"})
    return chat(messages)
