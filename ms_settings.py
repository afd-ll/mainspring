#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例设置：键 / 类型 / 默认值 / 校验 —— **一处权威**。

为什么单独一个文件：
  * 后端（校验 + 落库）、前端（渲染设置页）、自检探针都从这一份 SCHEMA 出发，
    免得出现「前端多了个下拉、后端不认识这个键」这种漂移。
  * 前端是**零构建的静态 HTML**，没法 import 这个模块 —— 所以它通过
    `GET /api/settings` 拿到的就是这份 SCHEMA 的 JSON 形态。

存储：SQLite 表 `settings(key, value, updated_at, updated_by)`，值一律存**字符串**。
  只落「用户改过的」键：读的时候用默认值打底 —— 这样以后改默认值，
  没动过设置的实例会自动跟上，而不是被一条陈旧的行钉死。

界面文案（每项的标题、说明、枚举项显示名）**不在这里** —— 那是前端 i18n 的活，
  同一件事只留一处权威。这里只管「有哪些键、什么类型、能填什么」。
"""
from __future__ import annotations

# 设置分组（前端按这个顺序出四块；文案在 web 的 I18N 里，键名用 settings.group.<g>）
GROUPS = ["appearance", "board", "security"]

# ---------------------------------------------------------------- 设置项定义
# type: enum / int / bool / text
SCHEMA: list[dict] = [
    # ---- 外观
    {"key": "lang", "group": "appearance", "type": "enum", "default": "zh",
     "choices": ["zh", "en"]},
    {"key": "theme", "group": "appearance", "type": "enum", "default": "auto",
     "choices": ["auto", "light", "dark"]},

    # ---- 看板显示
    {"key": "page_size", "group": "board", "type": "int", "default": "50",
     "choices": ["20", "50", "100", "200"]},
    {"key": "default_tab", "group": "board", "type": "enum", "default": "board",
     "choices": ["board", "projects", "events", "tokens", "agents", "settings"]},
    {"key": "time_format", "group": "board", "type": "enum", "default": "24h",
     "choices": ["24h", "12h"]},
    {"key": "relative_time", "group": "board", "type": "bool", "default": "1"},

    # ---- 安全
    {"key": "session_days", "group": "security", "type": "int", "default": "7",
     "min": 1, "max": 365},
    {"key": "token_default_role", "group": "security", "type": "enum", "default": "agent",
     "choices": ["readonly", "agent", "admin"]},
]

_BY_KEY = {s["key"]: s for s in SCHEMA}


def by_key() -> dict:
    return dict(_BY_KEY)


def defaults() -> dict:
    return {s["key"]: s["default"] for s in SCHEMA}


def schema_json() -> list:
    """给前端的形态：只出「结构」，不出文案。"""
    out = []
    for s in SCHEMA:
        item = {"key": s["key"], "group": s["group"], "type": s["type"],
                "default": s["default"]}
        for k in ("choices", "min", "max"):
            if k in s:
                item[k] = s[k]
        out.append(item)
    return out


def normalize(key: str, value) -> str:
    """把任意输入收敛成入库的字符串；非法就抛 ValueError（消息给前端直接用）。"""
    spec = _BY_KEY.get(key)
    if not spec:
        raise ValueError("未知设置项：%s" % key)

    t = spec["type"]
    raw = value
    if isinstance(raw, bool):
        raw = "1" if raw else "0"
    s = str("" if raw is None else raw).strip()

    if t == "bool":
        low = s.lower()
        if low in ("1", "true", "yes", "on"):
            return "1"
        if low in ("0", "false", "no", "off", ""):
            return "0"
        raise ValueError("%s 只能是 开 / 关" % key)

    if t == "int":
        try:
            n = int(s)
        except (TypeError, ValueError):
            raise ValueError("%s 必须是整数" % key)
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and n < lo:
            raise ValueError("%s 不能小于 %s" % (key, lo))
        if hi is not None and n > hi:
            raise ValueError("%s 不能大于 %s" % (key, hi))
        return str(n)

    if t == "enum":
        if s not in spec["choices"]:
            raise ValueError("%s 只能是 %s 之一" % (key, " / ".join(spec["choices"])))
        return s

    return s


def clean(patch: dict) -> dict:
    """校验一批设置，返回「收敛后的字符串值」。任一项非法就整体拒绝。"""
    if not isinstance(patch, dict):
        raise ValueError("设置必须是一个对象")
    out = {}
    for k, v in patch.items():
        out[str(k)] = normalize(str(k), v)
    return out


def is_default(key: str, value: str) -> bool:
    return str(_BY_KEY.get(key, {}).get("default", "")) == str(value)
