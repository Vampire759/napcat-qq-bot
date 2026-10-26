#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_client.py — 统一 AI 对话客户端: 外部 API 优先, 不可用时自动回退本地

被 listener.py(QQ群) 与 fnchat_ai.py(微信) 共用。调用顺序:

  1. 配置了外部 API(api_enabled=true 且 api_base_url 非空) -> 先走外部 API;
  2. 外部 API 超时 / 报错 / 返回空内容 / 未配置 -> 自动回退本地 llama.cpp;
  3. 两边都失败才抛异常, 由调用方决定怎么处理(一般是不回复并记日志)。

全部是 OpenAI 兼容的 /v1/chat/completions, 所以 DeepSeek / 通义 / 月之暗面 /
智谱 / OpenAI / 各类中转站 / Ollama / vLLM 都能直接用, 只改配置不改代码。

配置(conf/ai_config.json, 与原有键共存):

  "api_enabled": true,                 # 外部 API 总开关
  "api_base_url": "https://api.deepseek.com",  # 根地址, 带不带 /v1 都行
  "api_key": "sk-xxxx",                # 密钥(不入库, 只写本地 conf/)
  "api_model": "deepseek-chat",        # 外部模型名
  "api_timeout": 60,                   # 外部 API 超时(秒)
  "api_fallback_local": true,          # 外部失败时是否回退本地
  "api_proxy": ""                      # 可选, 如 http://127.0.0.1:7890

本地键沿用原来的 api_url / model / timeout / temperature / num_predict。

用法:

    import ai_client
    text = ai_client.chat(messages, AI_CFG, log=log.info)
    text, provider = ai_client.chat_ex(messages, AI_CFG, log=log.info)
"""

import json
import os
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

API_DEFAULTS = {
    "api_enabled": True,
    "api_base_url": "",
    "api_key": "",
    "api_model": "",
    "api_timeout": 60,
    "api_fallback_local": True,
    "api_proxy": "",
}

# 直连(不走系统代理)的 opener, 本机 llama.cpp 必须这样
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_proxy_openers = {}


def _opener(proxy=""):
    """按需返回 opener; proxy 为空=直连, 否则走指定 http(s) 代理。"""
    proxy = (proxy or "").strip()
    if not proxy:
        return _direct
    if proxy not in _proxy_openers:
        _proxy_openers[proxy] = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return _proxy_openers[proxy]


def _norm_base(base):
    """统一成不带 /v1 的根地址。"""
    base = (base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _val(cfg, key):
    cfg = cfg or {}
    v = cfg.get(key)
    return API_DEFAULTS[key] if v is None else v


def api_ready(cfg):
    """外部 API 是否可用(已开启且填了地址)。key 允许为空(本机网关可能不需要)。"""
    cfg = cfg or {}
    return bool(_val(cfg, "api_enabled") and _norm_base(_val(cfg, "api_base_url")))


def api_info(cfg):
    """给管理面板/日志用的外部 API 信息(不含完整密钥)。"""
    cfg = cfg or {}
    key = str(_val(cfg, "api_key") or "")
    return {
        "enabled": bool(_val(cfg, "api_enabled")),
        "ready": api_ready(cfg),
        "base_url": _norm_base(_val(cfg, "api_base_url")),
        "model": str(_val(cfg, "api_model") or ""),
        "key_hint": ("****" + key[-4:]) if key else "",
    }


def _post(url, payload, headers, timeout, proxy=""):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with _opener(proxy).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _content(data):
    choices = data.get("choices") or [{}]
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):      # 少数实现返回数组
        content = "".join(str(p.get("text", "")) for p in content
                          if isinstance(p, dict))
    return str(content or "").strip()


def _normalize(messages):
    """规范消息序列。

    群聊上下文会记录「没被回复的消息」, 容易出现连续多条 user;
    Gemma 等模型的聊天模板要求严格 user/assistant 交替, 否则 400。
    这里把连续同角色消息合并, 并丢掉开头的 assistant。
    """
    out = []
    for m in messages or []:
        role = (m or {}).get("role")
        content = (m or {}).get("content")
        if not role or content is None:
            continue
        content = str(content)
        if out and out[-1]["role"] == role and role != "system":
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    while len(out) > 1 and out[1]["role"] != "user":
        out.pop(1)
    return out


def _call(base, key, model, messages, timeout, temperature, max_tokens, proxy=""):
    url = _norm_base(base) + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + str(key)
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    return _content(_post(url, payload, headers, timeout, proxy))


def chat_ex(messages, cfg, log=None):
    """返回 (回答文本, "api"|"local")；两边都失败时抛 RuntimeError。"""
    cfg = cfg or {}
    messages = _normalize(messages)
    temperature = float(cfg.get("temperature", 0.7))
    max_tokens = int(cfg.get("num_predict", 512))
    api_error = ""

    if api_ready(cfg):
        base = _norm_base(_val(cfg, "api_base_url"))
        model = str(_val(cfg, "api_model") or cfg.get("model") or "")
        try:
            answer = _call(
                base, _val(cfg, "api_key"), model, messages,
                float(_val(cfg, "api_timeout")), temperature, max_tokens,
                _val(cfg, "api_proxy"))
            if answer:
                if log:
                    log(f"[AI] 外部 API 回答成功 ({base} / {model})")
                return answer, "api"
            api_error = f"外部 API 返回空内容 ({base})"
        except Exception as e:
            api_error = f"外部 API 调用失败 ({base}): {e}"
        if log:
            log(f"[AI] {api_error}")
        if not bool(_val(cfg, "api_fallback_local")):
            raise RuntimeError(api_error)
        if log:
            log("[AI] 回退本地模型")

    local_base = str(cfg.get("api_url") or "http://127.0.0.1:11435")
    local_model = str(cfg.get("model") or "local")
    try:
        answer = _call(
            local_base, "", local_model, messages,
            float(cfg.get("timeout", 180)), temperature, max_tokens, "")
    except Exception as e:
        reason = f"本地模型调用失败 ({local_base}): {e}"
        raise RuntimeError(f"{api_error}；{reason}" if api_error else reason) from e

    if not answer and log:
        log("[AI] 本地模型返回空内容")
    return answer, "local"


def chat(messages, cfg, log=None):
    """只要回答文本(与旧代码 _chat(msgs) 行为一致)。"""
    return chat_ex(messages, cfg, log=log)[0]


def test(cfg, prompt="你好，请用一句话介绍你自己。", log=None):
    """管理面板用: 主动测一次, 返回 (ok, provider, 文本或错误)。"""
    messages = [{"role": "user", "content": prompt}]
    try:
        answer, provider = chat_ex(messages, cfg, log=log)
        return True, provider, answer
    except Exception as e:
        return False, "none", str(e)
