# -*- coding: utf-8 -*-
"""v2 Agent 唤醒自测：起一个桩服务接 hook，验证「点名 → 真的 POST 出去」。"""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time

OUT = os.environ.get("MS_TEST_OUT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_ms_v2_agent_out.txt")
PORT = int(os.environ.get("MS_TEST_PORT") or 8901)
buf = []


def w(s=""):
    buf.append(str(s))
    print(s)


PASS = FAIL = 0


def ck(n, c, d=""):
    global PASS, FAIL
    if c:
        PASS += 1
        w("  [OK]   %-30s %s" % (n, d))
    else:
        FAIL += 1
        w("  [FAIL] %-30s %s" % (n, d))


class Hook(http.server.BaseHTTPRequestHandler):
    got = []
    protocol_version = "HTTP/1.1"

    def _send(self, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send({"ok": True, "who": "stub"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        Hook.got.append({"path": self.path, "body": body,
                         "event": self.headers.get("X-MS-Event", ""),
                         "token": self.headers.get("X-MS-Token", "")})
        self._send({"ok": True})

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Hook)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)

tmp = tempfile.mkdtemp(prefix="ms_agent_")
db = os.path.join(tmp, "t.sqlite3")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE if os.path.exists(os.path.join(_HERE, "ms_db.py")) else os.path.dirname(_HERE))
os.environ["MS_DB"] = db
import ms_db  # noqa: E402

ms_db.DB_PATH = db
ms_db.init_db(db)

try:
    w("=" * 60)
    w("Agent 唤醒自测（桩服务 127.0.0.1:%d）" % PORT)
    w("=" * 60)

    import urllib.request
    _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with _op.open("http://127.0.0.1:%d/ping" % PORT, timeout=4) as r:
            w("[0] 桩服务自检：http=%d（桩本身是通的）" % r.status)
    except Exception as e:
        w("[0] 🔴 桩服务自检失败：%s: %s —— 后面的 FAIL 都是这个引起的" % (type(e).__name__, e))

    w("[1] 表与状态")
    with ms_db.conn(db) as c:
        tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    ck("agents 表已建", "agents" in tabs, "tables=%s" % sorted(tabs))
    ck("状态含「已点名」", "已点名" in ms_db.STATUSES, str(ms_db.STATUSES))

    w("")
    w("[2] 登记唤醒目标")
    a = ms_db.add_agent("测试桩", "http://127.0.0.1:%d/hook" % PORT, token="ms_dummy", path=db)
    ck("add_agent", a.get("ok") and a.get("id") == 1, "id=%s" % a.get("id"))
    try:
        ms_db.add_agent("坏地址", "ftp://x/y", path=db)
        ck("坏 URL 被拒", False, "竟然通过了")
    except ms_db.MsError as e:
        ck("坏 URL 被拒", e.payload.get("error") == "bad url", e.payload.get("hint", ""))

    w("")
    w("[3] ★ fire_agents 真的 POST 出去")
    Hook.got.clear()
    payload = {"event": "mention", "code": "CX-05", "title": "测试条目",
               "opinion": "点名一下", "dashboard": "http://x/", "ts": ms_db.now()}
    res = ms_db.fire_agents(payload, path=db, timeout=5)
    ck("返回 1 条结果", len(res) == 1 and res[0]["status"] == "ok:200", str(res))
    ck("桩收到 1 次 POST", len(Hook.got) == 1, "收到 %d 次" % len(Hook.got))
    if Hook.got:
        g = Hook.got[0]
        ck("路径正确", g["path"] == "/hook", g["path"])
        ck("X-MS-Event 头", g["event"] == "mention", g["event"])
        ck("X-MS-Token 头透传", g["token"] == "ms_dummy", g["token"])
        try:
            j = json.loads(g["body"])
            ck("载荷含 code/title/opinion", j.get("code") == "CX-05" and j.get("opinion") == "点名一下",
               "code=%s title=%s" % (j.get("code"), j.get("title")))
        except Exception as e:
            ck("载荷是合法 JSON", False, str(e))

    with ms_db.conn(db) as c:
        r = c.execute("SELECT last_call_at,last_status,last_error FROM agents WHERE id=1").fetchone()
    ck("回写 last_status", r["last_status"] == "ok:200" and bool(r["last_call_at"]),
       "status=%s at=%s" % (r["last_status"], r["last_call_at"]))

    w("")
    w("[4] 失败路径")
    b = ms_db.add_agent("坏钩子", "http://127.0.0.1:1/nope", path=db)
    Hook.got.clear()
    res2 = ms_db.fire_agents(payload, path=db, timeout=3)
    ck("坏钩子不抛异常", len(res2) == 2, str([x["status"] for x in res2]))
    ck("坏钩子记 fail", any(x["status"] == "fail" for x in res2), str(res2[1]["error"])[:60])
    ck("好钩子仍成功", any(x["status"] == "ok:200" for x in res2), "")

    with ms_db.conn(db) as c:
        r2 = c.execute("SELECT last_status FROM agents WHERE id=?", (b["id"],)).fetchone()
    ck("坏钩子 last_status 已记", r2["last_status"] == "fail", r2["last_status"])

    w("")
    w("[5] 停用 / 单发 / 停用的不发")
    ms_db.update_agent(b["id"], enabled=False, path=db)
    Hook.got.clear()
    res3 = ms_db.fire_agents(payload, path=db, timeout=3)
    ck("停用后只发 1 个", len(res3) == 1, "发了 %d 个" % len(res3))

    Hook.got.clear()
    pr = ms_db.ping_agent(b["id"], path=db, timeout=3)
    ck("ping 停用目标也能单发", pr.get("status", "").startswith("fail"), str(pr)[:80])
    ck("ping 只发 1 次", len(Hook.got) == 0, "停用的是坏钩子，桩不应收到")

    Hook.got.clear()
    pr2 = ms_db.ping_agent(a["id"], path=db, timeout=3)
    ck("ping 好目标 ok", pr2.get("ok") and pr2.get("status") == "ok:200", str(pr2)[:70])
    ck("ping 只发 1 次", len(Hook.got) == 1, "收到 %d 次" % len(Hook.got))
    if Hook.got:
        ck("ping 的 event=test", Hook.got[0]["event"] == "test", Hook.got[0]["event"])

    w("")
    w("[6] 点名落状态（不依赖 hook）")
    it = ms_db.add_item(actor="t", path=db, **{"标题": "点名测试条", "类别": "VF"})
    code = it["编号"]
    r = ms_db.mention_item(code, "跑一下", path=db)
    g2 = ms_db.get_item(code, path=db)
    ck("点名 → 已点名", r["状态"] == "已点名" and g2["状态"] == "已点名", g2["状态"])
    ck("点名写入备注", "【点名 " in g2["备注"], "%d 字" % len(g2["备注"]))
    ev = ms_db.list_events(since=0, path=db)
    ck("落 mention 事件", any(e["kind"] == "mention" for e in ev["events"]),
       str([e["kind"] for e in ev["events"]]))

    w("")
    w("[7] 开关与删除")
    ms_db.update_agent(a["id"], enabled=False, path=db)
    ck("update enabled=false", ms_db.list_agents(path=db)[0]["enabled"] is False, "")
    ms_db.del_agent(a["id"], path=db)
    ck("del_agent", len(ms_db.list_agents(path=db)) == 1, "剩 %d 个" % len(ms_db.list_agents(path=db)))

    w("")
    w("=" * 60)
    w("结果：PASS %d / FAIL %d" % (PASS, FAIL))
    w("=" * 60)
except Exception:
    import traceback
    w("!!!!! 异常 !!!!!")
    w(traceback.format_exc())
finally:
    srv.shutdown()
    io.open(OUT, "w", encoding="utf-8", newline="\n").write("\n".join(buf) + "\n")
