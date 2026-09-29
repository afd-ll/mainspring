# -*- coding: utf-8 -*-
"""Mainspring 机枢 v2 · 数据层（SQLite）

唯一的真值源。v1 的 wb.json 只作为迁移输入，迁完即冻结。

设计要点：
  · 对外字段名保持**中文**（兼容 v1 页面 / MCP 入参 / 老大习惯），DB 列名用英文。
  · 任何写操作（增/改/闭环/点名/拍板/token 变更）都在**同一事务**里落一条 `events`。
    ⇒ 事件表 = "老大点名/拍板 → AI 同步收到" 的唯一机制。
  · items 的 code 唯一；编号按 AR/BG/CX/OP/VF 各类独立自增（沿用 AR-08 的体系）。
  · 排序沿用 v1：等级 → 截止日 → 编号自然序。

环境变量：
    MS_DB    SQLite 路径（默认 <本目录>/ms.sqlite3；自测用临时库隔离）
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

import ms_settings

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MS_DB") or os.path.join(HERE, "ms.sqlite3")

SCHEMA_VER = 3

# ---------------------------------------------------------------- 字段映射
FIELD_COLS = {
    "编号": "code",
    "标题": "title",
    "类型": "type",
    "状态": "status",
    "等级": "level",
    "项目": "project",
    "位置": "location",
    "父": "parent",
    "联动": "link",
    "截止日": "due",
    "备注": "note",
    "record_id": "record_id",
}
COL_FIELDS = {v: k for k, v in FIELD_COLS.items()}

# 中文名常量（被代码引用的字段才起名；字段本身是开放的）
F_CODE, F_TITLE, F_TYPE = "编号", "标题", "类型"
F_STATUS, F_LEVEL, F_PROJ = "状态", "等级", "项目"
F_NOTE, F_DUE, F_POS = "备注", "截止日", "位置"
F_PARENT, F_LINK, F_RID = "父", "联动", "record_id"

CATS = ("AR", "BG", "CX", "OP", "VF")
TYPES = ("待办", "待拍板", "验证记录")
# 🔴 状态链（2026-09-19 老大定）：待点名 → **已点名** → 进行中 → 已闭环 / 已否决
#    「已点名」= 老大点名了、等 agent 接；「进行中」= agent 真的在跑了。
STATUSES = ("待点名", "已点名", "进行中", "待拍板", "已闭环", "已否决")
LEVELS = ("红线", "高", "中", "低")
CLOSED = ("已闭环", "已否决")

LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}
DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"^([A-Za-z]*)-?(\d+)$")

# 事件类型
EV_ADD, EV_UPDATE, EV_CLOSE = "add", "update", "close"
EV_MENTION, EV_DECIDE = "mention", "decide"      # ★ 老大点名 / 拍板
EV_TOKEN, EV_MIGRATE = "token", "migrate"


class MsError(Exception):
    """带 error code 的业务异常（API / MCP 层转成结构化 JSON）。"""

    def __init__(self, error: str, **extra):
        super().__init__(error)
        self.payload = {"ok": False, "error": error, **extra}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ---------------------------------------------------------------- 建表
DDL = """
CREATE TABLE IF NOT EXISTS items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  code        TEXT NOT NULL UNIQUE,
  title       TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT '待办',
  status      TEXT NOT NULL DEFAULT '待点名',
  level       TEXT NOT NULL DEFAULT '中',
  project     TEXT NOT NULL DEFAULT '',
  location    TEXT NOT NULL DEFAULT '',
  parent      TEXT NOT NULL DEFAULT '',
  link        TEXT NOT NULL DEFAULT '',
  due         TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT '',
  record_id   TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT '',
  closed_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items(status);
CREATE INDEX IF NOT EXISTS ix_items_project ON items(project);

CREATE TABLE IF NOT EXISTS events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  actor    TEXT NOT NULL DEFAULT '',
  kind     TEXT NOT NULL,
  code     TEXT NOT NULL DEFAULT '',
  title    TEXT NOT NULL DEFAULT '',
  summary  TEXT NOT NULL DEFAULT '',
  payload  TEXT NOT NULL DEFAULT '',
  ack_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_ack ON events(ack_at);
CREATE INDEX IF NOT EXISTS ix_events_code ON events(code);

CREATE TABLE IF NOT EXISTS tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  token        TEXT NOT NULL UNIQUE,
  role         TEXT NOT NULL DEFAULT 'agent',
  note         TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT '',
  last_used_at TEXT NOT NULL DEFAULT '',
  revoked      INTEGER NOT NULL DEFAULT 0,
  last_pull_at   TEXT NOT NULL DEFAULT '',
  last_event_id  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  hook_url     TEXT NOT NULL DEFAULT '',
  token        TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  note         TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT '',
  last_call_at TEXT NOT NULL DEFAULT '',
  last_status  TEXT NOT NULL DEFAULT '',
  last_error   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT '');

-- 实例设置（界面语言 / 主题 / 看板显示 / 安全）。
-- 只存**用户改过的**键：读的时候用默认值打底，所以改默认值能惠及没动过设置的实例。
-- 键的合法集合与类型在 ms_settings.py 里（一处权威），这张表不管校验。
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT ''
);
"""


@contextmanager
def conn(path: str = None):
    """每次操作一个连接：WAL + 5s 忙等待（uvicorn 线程池并发写靠它兜底）。"""
    c = sqlite3.connect(path or DB_PATH, timeout=5.0)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _ensure_columns(c) -> list:
    """幂等补列（老库升级用）。SQLite 的 ADD COLUMN 没有 IF NOT EXISTS ⇒ 自己查 PRAGMA。"""
    added = []
    want = {
        "tokens": (("last_pull_at", "TEXT NOT NULL DEFAULT ''"),
                   ("last_event_id", "INTEGER NOT NULL DEFAULT 0")),
    }
    for table, cols in want.items():
        try:
            have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}
        except sqlite3.Error:
            continue
        for name, ddl in cols:
            if name not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
                added.append("%s.%s" % (table, name))
    return added


def init_db(path: str = None) -> dict:
    with conn(path) as c:
        c.executescript(DDL)
        added = _ensure_columns(c)
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_ver',?)", (str(SCHEMA_VER),))
    return {"ok": True, "db": path or DB_PATH, "schema_ver": SCHEMA_VER, "added": added}


def meta_get(k: str, default: str = "", path: str = None) -> str:
    with conn(path) as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(k: str, v: str, path: str = None) -> None:
    with conn(path) as c:
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


# ---------------------------------------------------------------- 实例设置
def settings_get_all(path: str = None) -> dict:
    """读全部设置：**默认值打底**，库里有的覆盖。

    只认 ms_settings.SCHEMA 里有的键 —— 旧版本遗留的键直接忽略，不让它漏给前端。
    """
    out = ms_settings.defaults()
    known = ms_settings.by_key()
    try:
        with conn(path) as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        if r["key"] in known:
            out[r["key"]] = r["value"]
    return out


def settings_rows(path: str = None) -> dict:
    """库里**真正存过的**键（前端标「改过」，自检/迁移也用得上）。"""
    try:
        with conn(path) as c:
            return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"],
                               "updated_by": r["updated_by"]}
                    for r in c.execute("SELECT * FROM settings").fetchall()}
    except sqlite3.Error:
        return {}


def settings_set(patch: dict, actor: str = "human", path: str = None) -> dict:
    """写设置。

    · 整批先校验（ms_settings.clean），任一项非法就整体拒绝 → ValueError
    · 值等于默认值时**删行**，回到「跟随默认」—— 否则一次误点就把默认值钉死了
    · 一次调用落**一条**汇总事件（跟其他写操作同一条规矩）
    """
    clean = ms_settings.clean(patch)
    before = settings_get_all(path)
    diffs = [(k, before.get(k, ""), v) for k, v in clean.items() if before.get(k) != v]
    if not diffs:
        return {"ok": True, "changed": [], "settings": before}

    with conn(path) as c:
        for k, _old, new in diffs:
            if ms_settings.is_default(k, new):
                c.execute("DELETE FROM settings WHERE key=?", (k,))
            else:
                c.execute("INSERT OR REPLACE INTO settings(key,value,updated_at,updated_by) "
                          "VALUES(?,?,?,?)", (k, new, now(), actor))
        log_event(c, "setting", actor=actor,
                  title="、".join(k for k, _, _ in diffs),
                  summary="；".join("%s %s → %s" % (k, o or "(默认)", n) for k, o, n in diffs),
                  payload={"changed": [{"key": k, "from": o, "to": n} for k, o, n in diffs]})
    return {"ok": True,
            "changed": [{"key": k, "from": o, "to": n} for k, o, n in diffs],
            "settings": settings_get_all(path)}


# ---------------------------------------------------------------- 行 <-> dict
def row_to_item(r) -> dict:
    d = {}
    for col in ("code", "title", "type", "status", "level", "project",
                "location", "parent", "link", "due", "note", "record_id"):
        d[COL_FIELDS[col]] = r[col] if r[col] is not None else ""
    d["created_at"] = r["created_at"]
    d["updated_at"] = r["updated_at"]
    d["closed_at"] = r["closed_at"]
    return d


def _norm(fields: dict) -> dict:
    """中文键 -> 列名键；丢弃空值 None。"""
    out = {}
    for k, v in fields.items():
        col = FIELD_COLS.get(k)
        if col and v is not None:
            out[col] = v
    return out


# ---------------------------------------------------------------- 事件
def log_event(c, kind: str, *, actor: str = "system", code: str = "",
              title: str = "", summary: str = "", payload=None) -> int:
    """在**当前事务**里落一条事件，返回事件 id（= AI 消费游标）。"""
    cur = c.execute(
        "INSERT INTO events(ts,actor,kind,code,title,summary,payload) VALUES(?,?,?,?,?,?,?)",
        (now(), actor, kind, code, title, summary,
         json.dumps(payload, ensure_ascii=False) if payload is not None else ""))
    return cur.lastrowid


def list_events(since: int = 0, unread_only: bool = False, limit: int = 200,
                path: str = None) -> dict:
    sql = "SELECT * FROM events WHERE id > ?"
    args = [int(since or 0)]
    if unread_only:
        sql += " AND ack_at = ''"
    sql += " ORDER BY id ASC LIMIT ?"
    args.append(int(limit or 200))
    with conn(path) as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
        last = c.execute("SELECT COALESCE(MAX(id),0) AS m FROM events").fetchone()["m"]
        unread = c.execute("SELECT COUNT(*) AS n FROM events WHERE ack_at=''").fetchone()["n"]
    for r in rows:
        try:
            r["payload"] = json.loads(r["payload"]) if r["payload"] else None
        except ValueError:
            pass
    return {"ok": True, "events": rows, "last_id": last, "unread": unread}


def last_event(path: str = None) -> dict:
    """最新一条事件（供写入后广播「有变化」信号）。"""
    with conn(path) as c:
        r = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return {}
    d = dict(r)
    try:
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
    except ValueError:
        pass
    return d


def ack_events(up_to: int = 0, actor: str = "agent", path: str = None) -> dict:
    """把 <= up_to 的未读事件标记已读；up_to=0 ⇒ 全部。"""
    with conn(path) as c:
        m = int(up_to or 0)
        if m <= 0:
            cur = c.execute("UPDATE events SET ack_at=? WHERE ack_at=''", (now(),))
        else:
            cur = c.execute("UPDATE events SET ack_at=? WHERE ack_at='' AND id<=?", (now(), m))
        n = cur.rowcount
    return {"ok": True, "acked": n, "actor": actor}


# ---------------------------------------------------------------- 编号 / 排序
def next_code(c, prefix: str = "AR") -> str:
    p = (prefix or "AR").strip().upper()
    if p not in CATS:
        raise MsError("bad category", field="类别", allowed=list(CATS))
    pat = p + "-%"
    mx = 0
    for r in c.execute("SELECT code FROM items WHERE code LIKE ?", (pat,)).fetchall():
        m = re.fullmatch(r"%s-(\d+)" % re.escape(p), str(r["code"]).strip())
        if m:
            mx = max(mx, int(m.group(1)))
    return "%s-%02d" % (p, mx + 1)


def _code_key(code: str):
    m = CODE_RE.match(str(code or ""))
    return (m.group(1), int(m.group(2))) if m else (str(code or ""), -1)


def _due_key(due: str):
    d = str(due or "").strip()
    return (1, "") if not d else (0, d)


def sort_items(items: list) -> list:
    return sorted(items, key=lambda it: (
        LEVEL_RANK.get(str(it.get(F_LEVEL, "")), len(LEVELS)),
        _due_key(it.get(F_DUE, "")),
        _code_key(it.get(F_CODE, "")),
    ))


# ---------------------------------------------------------------- 校验
_ENUMS = {F_TYPE: TYPES, F_STATUS: STATUSES, F_LEVEL: LEVELS}


def _check_enum(field, value):
    allowed = _ENUMS.get(field)
    if allowed is not None and str(value) not in allowed:
        raise MsError("bad enum", field=field, got=value, allowed=list(allowed))


def _check_due(value):
    v = str(value or "").strip()
    if v and not DUE_RE.match(v):
        raise MsError("bad date", field=F_DUE, got=v, hint="YYYY-MM-DD 或留空")


# ---------------------------------------------------------------- items 读
def _select(c, where="", args=()):
    return [row_to_item(r) for r in c.execute("SELECT * FROM items " + where, args).fetchall()]


def list_items(status=None, type=None, level=None, project=None, q=None,
               due_before=None, limit=200, path: str = None) -> list:
    with conn(path) as c:
        items = _select(c)
    def _split(v):
        return [p.strip() for p in re.split(r"[,，]", str(v)) if p.strip()] if v else []
    for field, want in ((F_STATUS, status), (F_TYPE, type), (F_LEVEL, level), (F_PROJ, project)):
        vals = _split(want)
        if vals:
            items = [it for it in items if str(it.get(field, "")) in vals]
    if due_before:
        d = str(due_before).strip()
        if not DUE_RE.match(d):
            raise MsError("bad date", detail="due_before 需要 YYYY-MM-DD", got=d)

        def _before(it):
            v = str(it.get(F_DUE, "")).strip()
            return bool(v) and bool(DUE_RE.match(v)) and v < d   # 空的/非日期的 ⇒ 不算"早于"

        items = [it for it in items if _before(it)]
    if q:
        needle = str(q).strip().lower()
        items = [it for it in items
                 if needle in str(it.get(F_CODE, "")).lower()
                 or needle in str(it.get(F_TITLE, "")).lower()
                 or needle in str(it.get(F_NOTE, "")).lower()]
    items = sort_items(items)
    if limit is not None and int(limit) >= 0:
        items = items[: int(limit)]
    return items


def get_item(code: str, path: str = None) -> dict:
    with conn(path) as c:
        r = c.execute("SELECT * FROM items WHERE code=?", (str(code or "").strip(),)).fetchone()
    return row_to_item(r) if r else {}


# ---------------------------------------------------------------- items 写
def add_item(actor: str = "system", path: str = None, **fields) -> dict:
    """新增一条。`类别` ∈ AR/BG/CX/OP/VF 决定编号前缀（默认 AR）。"""
    title = str(fields.get(F_TITLE) or "").strip()
    if not title:
        raise MsError("missing field", field=F_TITLE)
    cat = str(fields.pop("类别", "") or "").strip().upper()
    if cat and cat not in CATS:
        raise MsError("bad category", field="类别", allowed=list(CATS))
    if not cat:
        mh = re.match(r"^([A-Za-z]+)", str(fields.get(F_CODE, "") or ""))
        cand = mh.group(1).upper() if mh else ""
        cat = cand if cand in CATS else "AR"

    vals = {k: v for k, v in fields.items() if k not in (F_CODE, F_RID)}
    vals[F_TITLE] = title
    vals.setdefault(F_TYPE, "待办")
    vals.setdefault(F_LEVEL, "中")
    vals.setdefault(F_STATUS, "待点名")
    for f, d in ((F_POS, ""), (F_DUE, ""), (F_NOTE, ""), (F_PROJ, ""), (F_PARENT, ""), (F_LINK, "")):
        vals.setdefault(f, d)
    for f in _ENUMS:
        _check_enum(f, vals.get(f, ""))
    _check_due(vals.get(F_DUE))

    cols = _norm(vals)
    with conn(path) as c:
        code = next_code(c, cat)
        cols["code"] = code
        cols["created_at"] = cols["updated_at"] = now()
        keys = ",".join(cols)
        ph = ",".join("?" * len(cols))
        c.execute("INSERT INTO items(%s) VALUES(%s)" % (keys, ph), list(cols.values()))
        log_event(c, EV_ADD, actor=actor, code=code, title=title,
                  summary="新增 %s：%s" % (code, title),
                  payload={"type": vals.get(F_TYPE), "level": vals.get(F_LEVEL),
                           "project": vals.get(F_PROJ), "status": vals.get(F_STATUS)})
    return {"ok": True, F_CODE: code}


def update_item(code: str, actor: str = "system", path: str = None, **fields) -> dict:
    code = str(code or "").strip()
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        raise MsError("nothing to update", code=code)
    if F_CODE in fields or F_RID in fields:
        raise MsError("field is read-only", code=code,
                      fields=[k for k in (F_CODE, F_RID) if k in fields])
    with conn(path) as c:
        r = c.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
        if not r:
            raise MsError("not found", code=code)
        old = row_to_item(r)
        changed, diffs = [], {}
        for k, v in fields.items():
            _check_enum(k, v)
            if k == F_DUE:
                _check_due(v)
            if str(old.get(k, "")) != str(v):
                changed.append(FIELD_COLS[k])
                diffs[k] = [old.get(k, ""), v]
        if changed:
            cols = _norm(fields)
            sets = ",".join("%s=?" % k for k in cols)
            c.execute("UPDATE items SET %s, updated_at=? WHERE code=?" % sets,
                      list(cols.values()) + [now(), code])
            log_event(c, EV_UPDATE, actor=actor, code=code, title=old.get(F_TITLE, ""),
                      summary="改 %s：%s" % (code, _diff_text(diffs)),
                      payload={"changed": changed, "diff": diffs})
    return {"ok": True, "changed": [COL_FIELDS[k] for k in changed]}


def _diff_text(diffs: dict) -> str:
    parts = []
    for k, (a, b) in diffs.items():
        if k == F_NOTE:
            parts.append("备注 +%d 字" % len(str(b)))
        else:
            parts.append("%s %s→%s" % (k, a or "∅", b or "∅"))
    return " · ".join(parts)


def close_item(code: str, conclusion: str = None, actor: str = "system",
               path: str = None) -> dict:
    code = str(code or "").strip()
    with conn(path) as c:
        r = c.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
        if not r:
            raise MsError("not found", code=code)
        old = row_to_item(r)
        note = str(old.get(F_NOTE) or "")
        if conclusion:
            n = len(re.findall(r"【闭环 \d+】", note)) + 1
            note = note + "\n\n【闭环 %d】%s" % (n, str(conclusion).strip())
        c.execute("UPDATE items SET status=?, note=?, updated_at=?, closed_at=? WHERE code=?",
                  ("已闭环", note, now(), now(), code))
        log_event(c, EV_CLOSE, actor=actor, code=code, title=old.get(F_TITLE, ""),
                  summary="闭环 %s：%s" % (code, (str(conclusion or "").strip() or "（无结论）")[:80]),
                  payload={"conclusion": conclusion})
    return {"ok": True, F_CODE: code}


def mention_item(code: str, opinion: str = "", actor: str = "human",
                 path: str = None) -> dict:
    """★ 点名 = 老大说"这条要做" ⇒ 状态转「**已点名**」+ 记 mention 事件。

    调用方（API 层）随后会 fire_agents() 去**主动唤醒 agent**。
    """
    code = str(code or "").strip()
    opinion = str(opinion or "").strip()
    with conn(path) as c:
        r = c.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
        if not r:
            raise MsError("not found", code=code)
        old = row_to_item(r)
        note = str(old.get(F_NOTE) or "")
        if opinion:
            note = note + "\n\n【点名 %s】%s" % (time.strftime("%Y-%m-%d"), opinion)
        c.execute("UPDATE items SET status=?, note=?, updated_at=? WHERE code=?",
                  ("已点名", note, now(), code))
        log_event(c, EV_MENTION, actor=actor, code=code, title=old.get(F_TITLE, ""),
                  summary="点名 %s：%s" % (code, opinion or "（无附言）"),
                  payload={"opinion": opinion, "status": ["已点名", old.get(F_STATUS, "")]})
    return {"ok": True, F_CODE: code, "状态": "已点名"}


def decide_item(code: str, decision: str, actor: str = "human",
                path: str = None) -> dict:
    """★ 拍板 = 老大给出决定 ⇒ 意见写进备注 + 类型转回「待办」+ 记 decide 事件。"""
    code = str(code or "").strip()
    decision = str(decision or "").strip()
    if not decision:
        raise MsError("empty decision", code=code, hint="拍板必须给意见")
    with conn(path) as c:
        r = c.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
        if not r:
            raise MsError("not found", code=code)
        old = row_to_item(r)
        note = str(old.get(F_NOTE) or "")
        n = len(re.findall(r"【拍板 \d{4}-\d{2}-\d{2}】", note)) + 1
        note = note + "\n\n【拍板 %s】%s" % (time.strftime("%Y-%m-%d"), decision)
        status = old.get(F_STATUS, "")
        new_status = "进行中" if status in ("待点名", "待拍板") else status
        c.execute("UPDATE items SET type=?, status=?, note=?, updated_at=? WHERE code=?",
                  ("待办", new_status, note, now(), code))
        log_event(c, EV_DECIDE, actor=actor, code=code, title=old.get(F_TITLE, ""),
                  summary="拍板 %s：%s" % (code, decision),
                  payload={"decision": decision, "round": n,
                           "type": [old.get(F_TYPE, ""), "待办"],
                           "status": [status, new_status]})
    return {"ok": True, F_CODE: code, "类型": "待办", "状态": new_status}


# ---------------------------------------------------------------- 统计
def stats(path: str = None) -> dict:
    with conn(path) as c:
        items = _select(c)
        n_ev = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        n_unread = c.execute("SELECT COUNT(*) n FROM events WHERE ack_at=''").fetchone()["n"]
        n_tok = c.execute("SELECT COUNT(*) n FROM tokens WHERE revoked=0").fetchone()["n"]
    def cnt(field):
        m = {}
        for it in items:
            k = str(it.get(field, "") or "(空)")
            m[k] = m.get(k, 0) + 1
        return m
    open_items = [it for it in items if str(it.get(F_STATUS, "")) not in CLOSED]
    return {
        "ok": True,
        "total": len(items),
        "open": len(open_items),
        "status": cnt(F_STATUS),
        "level": cnt(F_LEVEL),
        "type": cnt(F_TYPE),
        "project": cnt(F_PROJ),
        "events": n_ev,
        "events_unread": n_unread,
        "tokens": n_tok,
    }


# ---------------------------------------------------------------- tokens
ROLES = ("admin", "agent", "readonly")


def new_token_value() -> str:
    return "ms_" + secrets.token_hex(24)


def add_token(name: str, role: str = "agent", note: str = "", token: str = None,
              actor: str = "system", path: str = None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise MsError("missing field", field="name")
    role = str(role or "agent").strip()
    if role not in ROLES:
        raise MsError("bad role", got=role, allowed=list(ROLES))
    tok = str(token or "").strip() or new_token_value()
    with conn(path) as c:
        c.execute("INSERT INTO tokens(name,token,role,note,created_at) VALUES(?,?,?,?,?)",
                  (name, tok, role, str(note or ""), now()))
        tid = c.execute("SELECT id FROM tokens WHERE token=?", (tok,)).fetchone()["id"]
        log_event(c, EV_TOKEN, actor=actor, summary="新建 token「%s」（%s）" % (name, role),
                  payload={"token_id": tid, "name": name, "role": role, "action": "add"})
    return {"ok": True, "id": tid, "name": name, "token": tok, "role": role}


def list_tokens(reveal: bool = True, path: str = None) -> list:
    with conn(path) as c:
        rows = c.execute("SELECT * FROM tokens ORDER BY id ASC").fetchall()
    out = []
    for r in rows:
        d = {"id": r["id"], "name": r["name"], "role": r["role"], "note": r["note"],
             "created_at": r["created_at"], "last_used_at": r["last_used_at"],
             "revoked": bool(r["revoked"]),
             "last_pull_at": r["last_pull_at"] if "last_pull_at" in r.keys() else "",
             "last_event_id": r["last_event_id"] if "last_event_id" in r.keys() else 0,
             "tail": r["token"][-6:]}
        # 🔴 隐私防护（老大 2026-09-20：「token 记得做隐私防护，前端不要明文」）
        #    reveal=False 时**一个字符都不给**，只留 tail；要明文走 get_token_value() 单取一次。
        d["token"] = r["token"] if reveal else ""
        out.append(d)
    return out


def get_token_value(tid: int, actor: str = "", reason: str = "", path: str = None) -> dict:
    """★ 单取一个 token 的明文（给「复制」按钮用）。

    🔴 隐私防护的第二半：明文**只在这一次请求里出现**，前端拿到直接写剪贴板、**不渲染**。
    每次取都记一条审计事件（谁、什么时候、取的是哪个）。
    """
    with conn(path) as c:
        r = c.execute("SELECT * FROM tokens WHERE id=?", (int(tid),)).fetchone()
        if not r:
            raise MsError("not found", id=tid)
        if r["revoked"]:
            raise MsError("token revoked", id=tid, name=r["name"])
        if actor:
            log_event(c, EV_TOKEN, actor=actor,
                      summary="取用 token「%s」的明文%s" % (r["name"], ("（%s）" % reason) if reason else ""),
                      payload={"token_id": int(tid), "name": r["name"], "action": "reveal"})
        return {"ok": True, "id": int(tid), "name": r["name"], "token": r["token"]}


def mark_pull(tid: int, last_event_id=None, path: str = None) -> None:
    """★ 记录某个 token「来拉活了」—— 这是"用 token 把握唤醒"的数据基础。

    · 只更新 `last_pull_at`（证明这个 agent 活着）
    · 给了 `last_event_id` 才更新游标（表示它确实消费到这里了）
    """
    with conn(path) as c:
        if last_event_id is None:
            c.execute("UPDATE tokens SET last_pull_at=? WHERE id=?", (now(), int(tid)))
        else:
            c.execute("UPDATE tokens SET last_pull_at=?, last_event_id=? WHERE id=?",
                      (now(), int(last_event_id), int(tid)))


def rotate_token(tid: int, actor: str = "human", path: str = None) -> dict:
    """替换：生成新值覆盖，旧值立即失效（无需重启）。"""
    tok = new_token_value()
    with conn(path) as c:
        r = c.execute("SELECT * FROM tokens WHERE id=?", (int(tid),)).fetchone()
        if not r:
            raise MsError("not found", id=tid)
        c.execute("UPDATE tokens SET token=?, revoked=0, last_used_at='' WHERE id=?",
                  (tok, int(tid)))
        log_event(c, EV_TOKEN, actor=actor, summary="替换 token「%s」的值（旧值立即失效）" % r["name"],
                  payload={"token_id": int(tid), "name": r["name"], "action": "rotate"})
    return {"ok": True, "id": int(tid), "name": r["name"], "token": tok}


def revoke_token(tid: int, actor: str = "human", path: str = None) -> dict:
    with conn(path) as c:
        r = c.execute("SELECT * FROM tokens WHERE id=?", (int(tid),)).fetchone()
        if not r:
            raise MsError("not found", id=tid)
        c.execute("UPDATE tokens SET revoked=1 WHERE id=?", (int(tid),))
        log_event(c, EV_TOKEN, actor=actor, summary="吊销 token「%s」" % r["name"],
                  payload={"token_id": int(tid), "name": r["name"], "action": "revoke"})
    return {"ok": True, "id": int(tid), "name": r["name"]}


def auth_token(token: str, touch: bool = True, path: str = None) -> dict:
    """鉴权：返回 token 行（含 role），无效/吊销返回 None。"""
    t = str(token or "").strip()
    if not t:
        return None
    with conn(path) as c:
        r = c.execute("SELECT * FROM tokens WHERE token=? AND revoked=0", (t,)).fetchone()
        if not r:
            return None
        if touch:
            c.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (now(), r["id"]))
        return {"id": r["id"], "name": r["name"], "role": r["role"]}


def verify_tokens(path: str = None) -> dict:
    """自检：每个未吊销 token 都过一遍鉴权函数并报结论。"""
    out = []
    for t in list_tokens(reveal=True, path=path):
        if t["revoked"]:
            out.append({**t, "verify": False, "why": "已吊销"})
            continue
        got = auth_token(t["token"], touch=False, path=path)
        out.append({**t, "verify": bool(got), "why": "OK" if got else "鉴权未通过"})
    ok = sum(1 for x in out if x["verify"])
    return {"ok": True, "passed": ok, "failed": len(out) - ok, "tokens": out}


# ---------------------------------------------------------------- 迁移
def import_json(path_json: str, actor: str = "migrate", db: str = None) -> dict:
    """把 v1 的 wb.json 导入（幂等：按 code 覆盖）。返回统计。"""
    with open(path_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise MsError("bad data file", path=path_json)
    n_new = n_upd = 0
    with conn(db) as c:
        c.executescript(DDL)
        for rec in data:
            code = str(rec.get(F_CODE) or "").strip()
            if not code:
                continue
            cols = _norm(rec)
            cols["code"] = code
            cols["created_at"] = cols.get("created_at") or now()
            cols["updated_at"] = now()
            if str(rec.get(F_STATUS) or "") in CLOSED:
                cols["closed_at"] = cols.get("closed_at") or now()
            exist = c.execute("SELECT id FROM items WHERE code=?", (code,)).fetchone()
            if exist:
                n_upd += 1
            else:
                n_new += 1
            keys = ",".join(cols)
            ph = ",".join("?" * len(cols))
            c.execute("INSERT OR REPLACE INTO items(id,%s) VALUES((SELECT id FROM items WHERE code=?),%s)"
                      % (keys, ph), [code] + list(cols.values()))
        log_event(c, EV_MIGRATE, actor=actor,
                  summary="从 wb.json 导入 %d 条（新增 %d / 覆盖 %d）" % (len(data), n_new, n_upd),
                  payload={"source": path_json, "total": len(data),
                           "new": n_new, "updated": n_upd})
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('migrated_from',?)", (path_json,))
    return {"ok": True, "total": len(data), "new": n_new, "updated": n_upd}


def ensure_admin_token(env_token: str, name: str = "v1-迁移", path: str = None) -> dict:
    """把旧 .env 的 MS_TOKEN 落库为一条 admin token（保证现有 agent 连接不断）。"""
    t = str(env_token or "").strip()
    if not t:
        return {"ok": False, "why": "空 token"}
    with conn(path) as c:
        r = c.execute("SELECT * FROM tokens WHERE token=?", (t,)).fetchone()
        if r:
            return {"ok": True, "existed": True, "id": r["id"]}
    got = add_token(name, role="admin", note="由 v1 .env::MS_TOKEN 迁移而来",
                    token=t, actor="migrate", path=path)
    return {"ok": True, "existed": False, **got}


# ---------------------------------------------------------------- 项目实体文件夹
# 老大（2026-09-16）：「真的作为实体文件夹保存每个项目」
#   ⇒ 每个「项目」一个目录，每条目一个 .md；新项目自动建目录。
#   ⚠️ **单向镜像**：sqlite 是唯一真值源，文件夹是可读导出（别反向改文件，会被覆盖）。
PROJECTS_ROOT = os.path.join(HERE, "projects")
_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe_name(text, limit: int = 48) -> str:
    t = _UNSAFE.sub("_", str(text or "")).strip().strip(".")
    t = re.sub(r"\s+", " ", t)
    if len(t) > limit:
        t = t[:limit].rstrip()
    return t or "untitled"


def _item_md(rec: dict) -> str:
    order = ["编号", "标题", "项目", "类型", "状态", "等级",
             "联动", "父", "位置", "截止日"]
    lines = ["---"]
    for k in order:
        v = rec.get(k)
        if v not in (None, ""):
            lines.append("%s: %s" % (k, str(v).replace("\n", " ")))
    for k in sorted(rec.keys()):
        if k in order or k in ("备注", "record_id", "created_at", "updated_at", "closed_at"):
            continue
        v = rec.get(k)
        if v not in (None, ""):
            lines.append("%s: %s" % (k, str(v).replace("\n", " ")))
    lines.append("---")
    lines.append("")
    lines.append(str(rec.get("备注") or "（无备注）"))
    lines.append("")
    return "\n".join(lines)


def sync_projects(items: list = None, path: str = None) -> dict:
    """按「项目」把条目镜像到 projects/<项目>/<编号>-<标题>.md。"""
    if items is None:
        items = list_items(limit=-1, path=path)
    root = os.environ.get("MS_PROJECTS") or PROJECTS_ROOT
    want, projs = {}, {}
    for rec in items:
        proj = str(rec.get(F_PROJ) or "").strip() or "_未归类"
        code = str(rec.get(F_CODE) or "").strip() or "no-code"
        rel = os.path.join(proj, "%s-%s.md" % (_safe_name(code, 24), _safe_name(rec.get(F_TITLE))))
        want[rel] = _item_md(rec)
        projs[proj] = projs.get(proj, 0) + 1

    written = 0
    for rel, text in want.items():
        full = os.path.join(root, rel)
        d = os.path.dirname(full)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        written += 1

    removed = 0
    keep = set(want.keys())
    if os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                full = os.path.join(dirpath, fn)
                if os.path.relpath(full, root) not in keep:
                    try:
                        os.remove(full)
                        removed += 1
                    except OSError:
                        pass
    if os.path.isdir(root):
        for dirpath, dirs, files in os.walk(root, topdown=False):
            if dirpath != root and not dirs and not files:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    return {"root": root, "written": written, "removed": removed, "projects": projs}


# ---------------------------------------------------------------- Agent 唤醒
# 老大（2026-09-19）：「我需要能够让我主动把"待点名"变成"已点名"，并且能够唤醒 ai agent 跑任务」
#   ⇒ agents 表登记"唤醒钩子"，点名时**主动 POST** 过去，把任务推给 agent。
#   ⚠️ 唤醒失败**绝不影响点名本身**（点名先落库、事件先记账，唤醒是 fire-and-forget）。
def add_agent(name: str, hook_url: str = "", token: str = "", note: str = "",
              actor: str = "human", path: str = None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise MsError("missing field", field="name")
    hook = str(hook_url or "").strip()
    if hook and not (hook.startswith("http://") or hook.startswith("https://")):
        raise MsError("bad url", field="hook_url", got=hook, hint="需 http(s):// 开头")
    with conn(path) as c:
        c.execute("INSERT INTO agents(name,hook_url,token,note,enabled,created_at) VALUES(?,?,?,?,1,?)",
                  (name, hook, str(token or ""), str(note or ""), now()))
        aid = c.execute("SELECT id FROM agents WHERE name=? ORDER BY id DESC", (name,)).fetchone()["id"]
        log_event(c, EV_TOKEN, actor=actor, summary="新增唤醒目标「%s」%s" % (name, hook or "（未配 hook）"),
                  payload={"agent_id": aid, "name": name, "action": "agent_add"})
    return {"ok": True, "id": aid, "name": name, "hook_url": hook}


def list_agents(reveal: bool = True, path: str = None) -> list:
    with conn(path) as c:
        rows = c.execute("SELECT * FROM agents ORDER BY id ASC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(r["enabled"])
        if not reveal and d["token"]:
            d["token"] = d["token"][:4] + "…"
        out.append(d)
    return out


def update_agent(aid: int, actor: str = "human", path: str = None, **fields) -> dict:
    allow = ("name", "hook_url", "token", "note", "enabled")
    cols = {k: v for k, v in fields.items() if k in allow and v is not None}
    if not cols:
        raise MsError("nothing to update", id=aid)
    if "enabled" in cols:
        cols["enabled"] = 1 if str(cols["enabled"]).lower() in ("1", "true", "yes", "on") else 0
    with conn(path) as c:
        r = c.execute("SELECT * FROM agents WHERE id=?", (int(aid),)).fetchone()
        if not r:
            raise MsError("not found", id=aid)
        sets = ",".join("%s=?" % k for k in cols)
        c.execute("UPDATE agents SET %s WHERE id=?" % sets, list(cols.values()) + [int(aid)])
        log_event(c, EV_TOKEN, actor=actor, summary="改唤醒目标「%s」：%s" % (r["name"], ",".join(cols)),
                  payload={"agent_id": int(aid), "action": "agent_update", "fields": list(cols)})
    return {"ok": True, "id": int(aid), "changed": list(cols)}


def del_agent(aid: int, actor: str = "human", path: str = None) -> dict:
    with conn(path) as c:
        r = c.execute("SELECT * FROM agents WHERE id=?", (int(aid),)).fetchone()
        if not r:
            raise MsError("not found", id=aid)
        c.execute("DELETE FROM agents WHERE id=?", (int(aid),))
        log_event(c, EV_TOKEN, actor=actor, summary="删除唤醒目标「%s」" % r["name"],
                  payload={"agent_id": int(aid), "action": "agent_del"})
    return {"ok": True, "id": int(aid)}


def mark_agent_call(aid: int, status: str, error: str = "", path: str = None) -> None:
    with conn(path) as c:
        c.execute("UPDATE agents SET last_call_at=?, last_status=?, last_error=? WHERE id=?",
                  (now(), status, error, int(aid)))


def _post_one(a: dict, payload: dict, timeout: float = 8.0) -> tuple:
    """打一个 hook，返回 (status, error)。**任何异常都吞掉**。

    🔴 **必须禁用代理**（`ProxyHandler({})`）—— 否则本机/内网 hook 会被 `http_proxy` 劫持：
    2026-09-19 实测，打 `127.0.0.1:1` 本应 `ConnectionRefused`，却拿到代理回的 **502 Bad Gateway**，
    而打本机桩服务直接 `RemoteDisconnected`。"唤醒 agent" 是**就近直连**语义，不该过代理。
    """
    import urllib.error
    import urllib.request

    hdr = {"Content-Type": "application/json",
           "X-MS-Event": str(payload.get("event") or "")}
    if a.get("token"):
        hdr["X-MS-Token"] = str(a["token"])
        hdr["Authorization"] = "Bearer " + str(a["token"])
    try:
        req = urllib.request.Request(a["hook_url"],
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=hdr, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return "ok:%d" % resp.status, ""
    except urllib.error.HTTPError as e:
        return "http:%d" % e.code, str(e)[:200]
    except Exception as e:                                        # noqa: BLE001
        return "fail", "%s: %s" % (type(e).__name__, str(e)[:160])


def fire_agents(payload: dict, path: str = None, timeout: float = 8.0) -> list:
    """主动唤醒：向所有 enabled 的 hook POST 一次。失败只记录，**不抛异常**。"""
    out = []
    for a in list_agents(reveal=True, path=path):
        if not a["enabled"] or not a["hook_url"]:
            continue
        status, err = _post_one(a, payload, timeout)
        try:
            mark_agent_call(a["id"], status, err, path=path)
        except Exception:                                         # noqa: BLE001
            pass
        out.append({"id": a["id"], "name": a["name"], "status": status, "error": err})
    return out


def ping_agent(aid: int, path: str = None, timeout: float = 8.0) -> dict:
    """只试打指定那一个（UI 上的「试一下」按钮）。"""
    a = None
    for x in list_agents(reveal=True, path=path):
        if x["id"] == int(aid):
            a = x
            break
    if not a:
        raise MsError("not found", id=aid)
    if not a["hook_url"]:
        return {"ok": False, "error": "no hook_url", "id": int(aid)}
    status, err = _post_one(a, {"event": "test", "code": "", "title": "工作台连通性测试",
                                "opinion": "这是一次测试唤醒，不需要真的跑任务。",
                                "ts": now()}, timeout)
    mark_agent_call(a["id"], status, err, path=path)
    return {"ok": status.startswith("ok"), "id": int(aid), "name": a["name"],
            "status": status, "error": err}
