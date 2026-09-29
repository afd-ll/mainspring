#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mainspring 机枢 · 运维探针 —— 一条命令验完所有的门

这支探针合并了原先散在仓库根目录的两份临时脚本：
    proto_test.py    MCP over HTTP 完整握手 + 三种鉴权
    _bg28_check.py   BG-28 回归：MCP 工具的形参名必须全是可读 ASCII

用法
    venv/bin/python tools/probe.py                        # 打 127.0.0.1:8787，凭据自动从 .env 读
    venv/bin/python tools/probe.py --url http://host:8787
    venv/bin/python tools/probe.py --no-db --no-human      # 只验 AI 通道

凭据（优先级：命令行 > 环境变量 > 仓库根 .env）
    MS_TOKEN              AI 通道令牌
    MS_USER / MS_PASS     人类通道账号（两个都有才验）

退出码
    0  全绿
    1  有 FAIL（先把输出里的 [FAIL] 看完再动服务）
    2  探针自己起不来（比如缺 httpx）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 本脚本在 tools/ 下，仓库根是上一级

_G, _Y, _R, _N = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    _G = _Y = _R = _N = ""

PASS, FAIL, SKIP = [], [], []


def ck(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  %s[OK]%s   %-42s %s" % (_G, _N, name, extra))
    else:
        FAIL.append(name)
        print("  %s[FAIL]%s %-42s %s" % (_R, _N, name, extra))
    return bool(cond)


def skip(name, why):
    SKIP.append(name)
    print("  %s[--]%s   %-42s %s" % (_Y, _N, name, why))


def head(t):
    print("\n" + t)


def read_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def sse_msgs(text):
    """streamable-http 可能回 SSE（`event: message` + `data: {...}`），也可能回裸 JSON。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except Exception:
                pass
    if not out:
        try:
            out.append(json.loads(text))
        except Exception:
            pass
    return out


class Mcp:
    """最小 MCP over streamable-HTTP 客户端：initialize -> tools/list -> tools/call"""

    def __init__(self, url, headers, timeout=25.0):
        import httpx
        self.url = url
        self.h = {"Content-Type": "application/json",
                  "Accept": "application/json, text/event-stream"}
        self.h.update(headers)
        self.sid = None
        self.c = httpx.Client(timeout=timeout)

    def close(self):
        self.c.close()

    def _post(self, payload):
        h = dict(self.h)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        r = self.c.post(self.url, headers=h, json=payload)
        if not self.sid:
            self.sid = r.headers.get("mcp-session-id")
        return r

    def initialize(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "ms-probe", "version": "1"}}})
        if r.status_code == 200:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def result(self, method, params=None, rid=2):
        r = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        for m in sse_msgs(r.text):
            if m.get("id") == rid:
                return r, m
        return r, None


def main() -> int:
    env = read_env(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser(description="Mainspring 机枢 · 运维探针")
    ap.add_argument("--url", default=os.environ.get("MS_PROBE_URL") or "http://127.0.0.1:8787")
    ap.add_argument("--token", default=os.environ.get("MS_TOKEN") or env.get("MS_TOKEN", ""))
    ap.add_argument("--user", default=os.environ.get("MS_USER") or env.get("MS_USER", ""))
    ap.add_argument("--pass", dest="pwd", default=os.environ.get("MS_PASS") or env.get("MS_PASS", ""))
    ap.add_argument("--db", default=os.environ.get("MS_DB") or os.path.join(ROOT, "ms.sqlite3"))
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--no-human", action="store_true")
    ap.add_argument("--timeout", type=float, default=25.0)
    a = ap.parse_args()

    try:
        import httpx  # noqa: F401
    except ImportError:
        print("探针需要 httpx：%s -m pip install httpx" % sys.executable)
        return 2

    base = a.url.rstrip("/")
    mcp_url = base + "/mcp"
    tok = (a.token or "").strip()

    print("=" * 76)
    print("Mainspring 机枢 · 运维探针")
    print("  目标  %s" % base)
    print("  令牌  %s" % (("已提供（...%s）" % tok[-6:]) if tok else "未提供 —— 只验非鉴权项"))
    print("  库    %s" % ("跳过（--no-db）" if a.no_db else a.db))
    print("=" * 76)

    # ---------------- 1 探活 ----------------
    head("[1] 探活")
    try:
        with httpx.Client(timeout=a.timeout) as c:
            r = c.get(base + "/health")
        try:
            j = r.json()
        except Exception:
            j = {}
        ck("GET /health -> 200", r.status_code == 200, "HTTP %s" % r.status_code)
        ck("/health 报出服务与版本", bool(j.get("server")),
           "server=%s ver=%s" % (j.get("server"), j.get("ver")))
    except Exception as e:
        ck("GET /health", False, "%s: %s" % (type(e).__name__, e))
        print("\n服务没起来，后面不用验了。")
        return 1

    # ---------------- 2 AI 通道 ----------------
    head("[2] AI 通道 · 鉴权")

    def probe_mcp(label, headers=None, url=None):
        cli = None
        try:
            cli = Mcp(url or mcp_url, headers or {}, a.timeout)
            r = cli.initialize()
            nt = -1
            if r.status_code == 200:
                _, m = cli.result("tools/list")
                try:
                    nt = len(m["result"]["tools"])
                except Exception:
                    nt = -1
            return r.status_code, nt
        except Exception as e:
            return "%s: %s" % (type(e).__name__, e), -1
        finally:
            if cli:
                cli.close()

    if tok:
        for label, kw in (("X-MS-Token", dict(headers={"X-MS-Token": tok})),
                          ("Bearer", dict(headers={"Authorization": "Bearer " + tok})),
                          ("?token=", dict(url=mcp_url + "?token=" + tok))):
            code, nt = probe_mcp(label, **kw)
            ck("鉴权 %-11s -> initialize 200" % label, code == 200, "HTTP %s" % code)
            if code == 200:
                ck("鉴权 %-11s -> tools/list 非空" % label, nt > 0, "工具数 %s" % nt)
        code, _ = probe_mcp("wrong", headers={"X-MS-Token": "definitely-not-a-token"})
        ck("错 token -> 401", code == 401, "HTTP %s" % code)
        code, _ = probe_mcp("none", headers={})
        ck("无凭据 -> 401", code == 401, "HTTP %s" % code)
    else:
        skip("三种鉴权", "没提供 MS_TOKEN")
        code, _ = probe_mcp("none", headers={})
        ck("无凭据 -> 401", code == 401, "HTTP %s" % code)

    # ---------------- 3 工具 schema（BG-28） ----------------
    head("[3] MCP 工具清单 · BG-28（形参名必须可读 ASCII）")
    if tok:
        cli = Mcp(mcp_url, {"X-MS-Token": tok}, a.timeout)
        try:
            r = cli.initialize()
            if r.status_code != 200:
                ck("MCP 握手", False, "HTTP %s" % r.status_code)
            else:
                ck("MCP 握手", True, "protocol=%s" % r.headers.get("mcp-protocol-version", "?"))
                _, m = cli.result("tools/list")
                tools = ((m or {}).get("result") or {}).get("tools") or []
                ck("tools/list 有工具", len(tools) > 0, "%d 个" % len(tools))
                bad, thin = [], []
                for t in tools:
                    sch = t.get("inputSchema") or {}
                    props = sch.get("properties") or {}
                    for k in props:
                        if not k.isascii():
                            bad.append("%s.%s" % (t.get("name"), k))
                    for k in (sch.get("required") or []):
                        if k not in props:
                            thin.append("%s.%s" % (t.get("name"), k))
                ck("所有形参名是可读 ASCII", not bad, "可疑：%s" % bad[:6])
                ck("required 都在 properties 里", not thin, "缺：%s" % thin[:6])
                print("      工具：" + " ".join(t.get("name", "?") for t in tools))
                _, m2 = cli.result("tools/call", {"name": "ms_list", "arguments": {}})
                try:
                    lst = json.loads(m2["result"]["content"][0]["text"])
                    # 空库不算错 —— 这里验的是"这条路通不通"，不是"有没有数据"
                    if isinstance(lst, list):
                        if lst:
                            ck("ms_list 真的读得回条目", True, "%d 条" % len(lst))
                        else:
                            skip("ms_list 真的读得回条目", "库是空的（新装就是这样）")
                    else:
                        ck("ms_list 真的读得回条目", False, "返回的不是列表：%.80r" % (lst,))
                except Exception as e:
                    ck("ms_list 真的读得回条目", False, "%s: %s" % (type(e).__name__, e))
        finally:
            cli.close()
    else:
        skip("工具清单 / BG-28", "没提供 MS_TOKEN")

    # ---------------- 4 人类通道 ----------------
    head("[4] 人类通道（浏览器那扇门）")
    if a.no_human or not (a.user and a.pwd):
        skip("人类通道", "没给 MS_USER / MS_PASS（或 --no-human）")
    else:
        try:
            with httpx.Client(timeout=a.timeout) as c:
                r = c.get(base + "/")
                ck("GET / -> 200", r.status_code == 200,
                   "HTTP %s %d 字节" % (r.status_code, len(r.content)))
                ck("/ 是 HTML 前端", "text/html" in (r.headers.get("content-type") or ""),
                   r.headers.get("content-type"))
                r = c.get(base + "/api/items")
                ck("无凭据 /api/items -> 401", r.status_code == 401, "HTTP %s" % r.status_code)
                r = c.post(base + "/login", json={"user": a.user, "pass": "wrong-password"})
                ck("错密码 /login -> 401", r.status_code == 401, "HTTP %s" % r.status_code)
                r = c.post(base + "/login", json={"user": a.user, "pass": a.pwd})
                got = "ms_session" in c.cookies
                ck("对密码 /login -> 200 + Cookie", r.status_code == 200 and got,
                   "HTTP %s cookie=%s" % (r.status_code, got))
                if got:
                    r = c.get(base + "/api/whoami")
                    ck("带 Cookie /api/whoami -> 200", r.status_code == 200, "HTTP %s" % r.status_code)
                    r = c.get(base + "/api/items")
                    n = -1
                    try:
                        n = r.json().get("count", -1)
                    except Exception:
                        pass
                    ck("带 Cookie /api/items -> 200", r.status_code == 200,
                       "HTTP %s count=%s" % (r.status_code, n))
                    # 同上：能解析出 count 就说明这条路是通的
                    ck("/api/items 的 count 可解析", n >= 0, "count=%s" % n)
        except Exception as e:
            ck("人类通道", False, "%s: %s" % (type(e).__name__, e))

    # ---------------- 5 数据库 ----------------
    head("[5] 数据库（真值源）")
    if a.no_db:
        skip("SQLite 检查", "--no-db")
    elif not os.path.exists(a.db):
        skip("SQLite 检查", "找不到 %s" % a.db)
    else:
        con = None
        try:
            con = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
            tabs = [x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            ck("库可读", True, "%.2f MB" % (os.path.getsize(a.db) / 1e6))
            need = ("items", "events", "tokens", "agents")
            ck("表齐全 %s" % "/".join(need), all(t in tabs for t in need), "实际 %s" % sorted(tabs))
            ni = con.execute("SELECT count(*) FROM items").fetchone()[0] if "items" in tabs else -1
            ne = con.execute("SELECT count(*) FROM events").fetchone()[0] if "events" in tabs else -1
            # 空库不是故障 —— 新装起来就是这样，别把新人吓一跳
            if ni > 0:
                ck("items 有数据", True, "%s 条" % ni)
            else:
                skip("items 有数据", "库是空的（新装就是这样）")
            if ne > 0:
                ck("events 有数据", True, "%s 条" % ne)
            else:
                skip("events 有数据", "还没有任何事件（写一条就有了）")
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
            ck("integrity_check -> ok", ic == "ok", str(ic))
        except Exception as e:
            ck("SQLite 检查", False, "%s: %s" % (type(e).__name__, e))
        finally:
            if con:
                con.close()

    print("\n" + "=" * 76)
    print("PASS %d · FAIL %d · SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    for n in FAIL:
        print("  %sFAIL%s  %s" % (_R, _N, n))
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
