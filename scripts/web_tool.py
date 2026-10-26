#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_tool.py — 给 AI 用的轻量联网工具(网页搜索 + 链接正文抽取)

纯标准库实现, 不需要 API Key。由 conf/ai_config.json 控制, 默认开启:

  "web_enabled": true        总开关; false = 完全关闭联网(纯本地模型回答)
  "web_engine": "bing"       bing(默认, 国内可直连) | duckduckgo
  "web_timeout": 8           单次请求超时(秒)
  "web_max_context": 1200    注入给模型的检索文本上限(字符)
  "web_cache_ttl": 600       同一问题缓存秒数(避免重复请求)

用法(供 listener.py / fnchat_ai.py 调用):

    import web_tool
    ctx = web_tool.web_context(text, AI_CFG)   # 不需要联网时返回 ""
"""

import html as _html
import json
import os
import re
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONF_DIR = os.path.join(ROOT_DIR, "conf")

WEB_DEFAULTS = {
    "web_enabled": True,
    "web_engine": "bing",
    "web_timeout": 8,
    "web_max_context": 1200,
    "web_cache_ttl": 600,
}

# 直连(不走系统代理), 避免代理环境导致搜索失败
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_MAX_BYTES = 600 * 1024          # 单个页面最多读 600KB, 防内存暴涨

URL_RE = re.compile(r"https?://[^\s<>\"'\u3000]+")
# 明确要求联网的说法
EXPLICIT = ("搜索", "搜一下", "搜一搜", "查一下", "查一查", "帮我查", "联网",
            "上网查", "网上查", "百度一下", "谷歌一下")
# 明显有时效性的话题
TIMELY = ("今天", "现在", "最新", "最近", "新闻", "天气", "气温", "价格",
          "多少钱", "汇率", "股价", "几点", "日期", "今年", "昨天", "明天",
          "发布", "版本", "赛况", "比分")
# 疑问信号(需配合问号/吗, 且句子够长才触发, 避免"吃了吗"这种闲聊)
QUESTION = ("什么", "是谁", "为什么", "怎么", "如何", "多少", "哪", "吗",
            "?", "？")

_cache = {}          # key -> (ts, context)
_MAX_CACHE = 200


def _cfg_val(cfg, key):
    if cfg is None:
        return WEB_DEFAULTS[key]
    return cfg.get(key, WEB_DEFAULTS[key])


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read(_MAX_BYTES).decode("utf-8", "replace")


def _strip_tags(s):
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", "", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def needs_web(text):
    """判断这条消息是否值得联网(默认开启时由启发式决定, 避免每条闲聊都搜索)。"""
    t = (text or "").strip()
    if len(t) < 4:
        return False
    if URL_RE.search(t):
        return True
    if any(k in t for k in EXPLICIT):
        return True
    if any(k in t for k in TIMELY):
        return True
    has_q = any(k in t for k in QUESTION)
    return has_q and len(t) >= 8


def _bing(query, timeout, count=4):
    url = ("https://cn.bing.com/search?q=" + urllib.parse.quote(query)
           + "&count=10&setlang=zh-CN")
    page = _http_get(url, timeout)
    blocks = re.findall(r'<li class="b_algo".*?</li>', page, flags=re.S)[:count]
    out = []
    for b in blocks:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                      b, flags=re.S)
        if not m:
            continue
        title = _strip_tags(m.group(2))
        link = _html.unescape(m.group(1))
        sn = re.search(r"<p[^>]*>(.*?)</p>", b, flags=re.S)
        snippet = _strip_tags(sn.group(1)) if sn else ""
        if title:
            out.append((title, link, snippet))
    return out


def _ddg(query, timeout, count=4):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    page = _http_get(url, timeout)
    links = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        page, flags=re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>',
                          page, flags=re.S)
    out = []
    for i, (link, title) in enumerate(links[:count]):
        link = _html.unescape(link)
        # DDG 有时给跳转链接, 取出真实地址
        if "uddg=" in link:
            try:
                link = urllib.parse.unquote(
                    urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["uddg"][0])
            except Exception:
                pass
        snippet = _strip_tags(snippets[i]) if i < len(snippets) else ""
        t = _strip_tags(title)
        if t:
            out.append((t, link, snippet))
    return out


def fetch_page(url, timeout=8, max_chars=1500):
    """抓取一个网页: 返回 (标题, 正文摘录)。"""
    page = _http_get(url, timeout)
    m = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
    title = _strip_tags(m.group(1)) if m else url
    body = re.search(r"<body[^>]*>(.*?)</body>", page, flags=re.S | re.I)
    text = _strip_tags(body.group(1) if body else page)
    return title, text[:max_chars]


def search(query, cfg=None, count=4):
    """主动搜索(供 Agent 工具调用): 返回格式化文本, 失败或联网关闭时返回 ""。"""
    if not bool(_cfg_val(cfg, "web_enabled")):
        return ""
    timeout = float(_cfg_val(cfg, "web_timeout"))
    limit = int(_cfg_val(cfg, "web_max_context"))
    engine = str(_cfg_val(cfg, "web_engine")).lower()
    try:
        results = (_ddg if engine == "duckduckgo" else _bing)(query, timeout, count)
    except Exception:
        return ""
    if not results:
        return ""
    lines = [f"搜索: {query}"]
    for i, (title, link, sn) in enumerate(results, 1):
        lines.append(f"{i}. {title}\n   {sn}\n   {link}")
    ctx = "\n".join(lines)
    return ctx[:limit].rstrip() + "…" if len(ctx) > limit else ctx


def web_context(text, cfg=None):
    """需要联网时返回可注入提示词的检索结果文本; 否则返回 ""。"""
    if not bool(_cfg_val(cfg, "web_enabled")):
        return ""
    t = (text or "").strip()
    if not needs_web(t):
        return ""

    timeout = float(_cfg_val(cfg, "web_timeout"))
    limit = int(_cfg_val(cfg, "web_max_context"))
    ttl = float(_cfg_val(cfg, "web_cache_ttl"))
    key = re.sub(r"\s+", " ", t.lower())[:200]

    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    ctx = ""
    try:
        m = URL_RE.search(t)
        if m:
            title, body = fetch_page(m.group(0), timeout)
            ctx = f"来源: {m.group(0)}\n标题: {title}\n正文摘录:\n{body}"
        else:
            engine = str(_cfg_val(cfg, "web_engine")).lower()
            results = (_ddg if engine == "duckduckgo" else _bing)(t, timeout)
            if results:
                lines = ["以下为联网检索到的公开资料(可能过时或不完整):"]
                for i, (title, link, sn) in enumerate(results, 1):
                    lines.append(f"{i}. {title}\n   {sn}\n   {link}")
                ctx = "\n".join(lines)
    except Exception:
        ctx = ""

    if len(ctx) > limit:
        ctx = ctx[:limit].rstrip() + "…"
    if ctx:
        _cache[key] = (time.time(), ctx)
        if len(_cache) > _MAX_CACHE:
            for k in sorted(_cache, key=lambda x: _cache[x][0])[:50]:
                _cache.pop(k, None)
    return ctx
