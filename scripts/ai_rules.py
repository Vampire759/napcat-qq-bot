#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_rules.py — AI 规则限制(默认关闭)

开启后:
  1. conf/ai_rules.md 的内容会以「必须遵守的规则」追加到系统提示词，优先级高于人格设定；
  2. conf/ai_config.json 里 rules_block_keywords 命中的用户消息直接不回复(硬限制)。

配置(conf/ai_config.json):
  "rules_enabled": false        总开关, 默认关闭
  "rules_file": "conf/ai_rules.md"
  "rules_block_keywords": []    违禁词列表, 命中即拒答(不回复)

规则 md 支持热加载: 改完保存即生效, 无需重启。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONF_DIR = os.path.join(ROOT_DIR, "conf")

_cache = {"path": None, "mtime": 0.0, "text": ""}


def rules_path(cfg):
    p = str((cfg or {}).get("rules_file") or "conf/ai_rules.md")
    return p if os.path.isabs(p) else os.path.join(ROOT_DIR, p)


def rules_text(cfg):
    """返回规则文本; 未开启或文件不存在时返回 ""。"""
    if not bool((cfg or {}).get("rules_enabled", False)):
        return ""
    path = rules_path(cfg)
    try:
        m = os.path.getmtime(path)
        if _cache["path"] != path or m != _cache["mtime"]:
            with open(path, "r", encoding="utf-8") as f:
                _cache.update({"path": path, "mtime": m, "text": f.read().strip()})
    except Exception:
        return ""
    return _cache["text"]


def blocked(text, cfg):
    """命中违禁词返回该词, 否则返回 ""(仅 rules_enabled 开启时生效)。"""
    if not bool((cfg or {}).get("rules_enabled", False)):
        return ""
    for kw in (cfg or {}).get("rules_block_keywords") or []:
        kw = str(kw).strip()
        if kw and kw in (text or ""):
            return kw
    return ""
