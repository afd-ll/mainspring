# -*- coding: utf-8 -*-
"""ms_db.py 的单元测试 —— 数据层是真值源，之前只有端到端探针间接覆盖它。

跑法：
    venv/bin/python tests/test_db.py

全程跑在**临时库**上（tempfile.mkdtemp），首尾各取一次真库指纹做隔离自证：
真库 ms.sqlite3 的 size/mtime 前后必须一致。
"""
import io
import json
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="msdb_")
DB = os.path.join(TMP, "t.sqlite3")
os.environ["MS_DB"] = DB
os.environ["MS_PROJECTS"] = os.path.join(TMP, "projects")

import ms_db  # noqa: E402

ms_db.DB_PATH = DB

REAL_DB = os.path.join(ROOT, "ms.sqlite3")
_OUT = os.environ.get("MS_TEST_OUT") or os.path.join(_HERE, "_test_db_out.txt")

buf = []
PASS = FAIL = 0


def w(s=""):
    buf.append(str(s))
    print(s)


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        w("  [OK]   %-42s %s" % (name, detail))
    else:
        FAIL += 1
        w("  [FAIL] %-42s %s" % (name, detail))


def fingerprint(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def err_of(fn, *a, **kw):
    """跑一个应当抛 MsError 的调用，返回它的 error code（没抛返回 None）。"""
    try:
        fn(*a, **kw)
        return None
    except ms_db.MsError as e:
        return e.payload.get("error")
    except Exception as e:                                        # noqa: BLE001
        return "WRONG-EXC:%s" % type(e).__name__


def all_events(path=DB):
    return ms_db.list_events(since=0, limit=100000, path=path)["events"]


def n_events(path=DB):
    return ms_db.list_events(since=0, limit=100000, path=path)["last_id"]


real_before = fingerprint(REAL_DB)

try:
    w("=" * 68)
    w("ms_db.py 单元测试（临时库 %s）" % TMP)
    w("真库指纹（前后必须一致）：%s" % (real_before,))
    w("=" * 68)

    # ------------------------------------------------------------ [1] 建库
    w("")
    w("[1] 建库 / 幂等 / 老库补列 / meta")
    r = ms_db.init_db(DB)
    ck("首次 init_db ok", r.get("ok") is True, "schema_ver=%s" % r.get("schema_ver"))
    ck("schema_ver = 2", r.get("schema_ver") == ms_db.SCHEMA_VER, str(ms_db.SCHEMA_VER))
    with ms_db.conn(DB) as c:
        tabs = sorted(x[0] for x in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    ck("五张业务表齐全", all(t in tabs for t in ("items", "events", "tokens", "agents", "meta")),
       str(tabs))
    r2 = ms_db.init_db(DB)
    ck("二次 init_db 幂等（无补列）", r2.get("added") == [], str(r2.get("added")))

    db2 = os.path.join(TMP, "old.sqlite3")
    with ms_db.conn(db2) as c:
        c.execute("""CREATE TABLE tokens (id INTEGER PRIMARY KEY, name TEXT, token TEXT UNIQUE,
                     role TEXT, note TEXT, created_at TEXT, last_used_at TEXT,
                     revoked INTEGER NOT NULL DEFAULT 0)""")
    r3 = ms_db.init_db(db2)
    ck("老库自动补两列（升级路径）",
       sorted(r3.get("added") or []) == ["tokens.last_event_id", "tokens.last_pull_at"],
       str(r3.get("added")))

    ms_db.meta_set("k1", "v1", path=DB)
    ck("meta_set/get 往返", ms_db.meta_get("k1", path=DB) == "v1", ms_db.meta_get("k1", path=DB))
    ck("meta_get 缺省值", ms_db.meta_get("nope", "dflt", path=DB) == "dflt", "dflt")

    # ------------------------------------------------------------ [2] 编号
    w("")
    w("[2] 编号分配（各类独立自增）")
    codes = {}
    for cat in ms_db.CATS:
        for i in range(1, 3 if cat == "AR" else 2):
            it = ms_db.add_item(actor="t", path=DB, **{"标题": "%s 第%d条" % (cat, i), "类别": cat})
            codes.setdefault(cat, []).append(it["编号"])
    ck("AR 独立自增 AR-01/AR-02", codes["AR"] == ["AR-01", "AR-02"], str(codes["AR"]))
    ck("BG/CX/OP/VF 各自从 01 起",
       [codes[c][0] for c in ("BG", "CX", "OP", "VF")] == ["BG-01", "CX-01", "OP-01", "VF-01"],
       str([codes[c][0] for c in ("BG", "CX", "OP", "VF")]))
    ck("编号格式 前缀-两位", all(re.fullmatch(r"[A-Z]{2}-\d{2}", x) for v in codes.values() for x in v),
       "示例 %s" % codes["AR"][0])

    it = ms_db.add_item(actor="t", path=DB, **{"标题": "靠编号推类别", "编号": "BG-99"})
    ck("不传类别时按「编号」前缀推", it["编号"] == "BG-02", it["编号"])
    it = ms_db.add_item(actor="t", path=DB, **{"标题": "无任何提示"})
    ck("无提示默认 AR", it["编号"].startswith("AR-"), it["编号"])
    ck("非法类别被拒", err_of(ms_db.add_item, actor="t", path=DB,
                              **{"标题": "x", "类别": "ZZ"}) == "bad category", "")
    ck("缺标题被拒", err_of(ms_db.add_item, actor="t", path=DB, **{"类别": "AR"}) == "missing field", "")

    # ------------------------------------------------------------ [3] 枚举
    w("")
    w("[3] 枚举 / 日期闸门")
    ck("非法类型被拒", err_of(ms_db.add_item, actor="t", path=DB,
                              **{"标题": "x", "类型": "乱写"}) == "bad enum", "")
    ck("非法等级被拒", err_of(ms_db.add_item, actor="t", path=DB,
                              **{"标题": "x", "等级": "超级高"}) == "bad enum", "")
    ck("非法截止日被拒", err_of(ms_db.add_item, actor="t", path=DB,
                                **{"标题": "x", "截止日": "2026/1/1"}) == "bad date", "")
    ok_all = True
    for st in ms_db.STATUSES:
        for lv in ms_db.LEVELS:
            try:
                ms_db.add_item(actor="t", path=DB, **{"标题": "枚举 %s/%s" % (st, lv),
                                                      "状态": st, "等级": lv, "类别": "VF"})
            except Exception as e:                                # noqa: BLE001
                ok_all = False
                w("     %s/%s -> %s" % (st, lv, e))
    ck("6 状态 × 4 等级全合法", ok_all, "24 种组合")
    it = ms_db.add_item(actor="t", path=DB, **{"标题": "日期 ok", "截止日": "2026-12-31", "类别": "CX"})
    ck("合法日期通过", ms_db.get_item(it["编号"], path=DB)["截止日"] == "2026-12-31", "")

    # ------------------------------------------------------------ [4] 排序
    w("")
    w("[4] 排序（纯函数，不经库）")
    fake = [
        {"编号": "AR-10", "等级": "高", "截止日": ""},
        {"编号": "AR-02", "等级": "高", "截止日": ""},
        {"编号": "BG-01", "等级": "红线", "截止日": ""},
        {"编号": "CX-01", "等级": "高", "截止日": "2030-01-01"},
        {"编号": "OP-01", "等级": "高", "截止日": "2026-01-01"},
        {"编号": "VF-01", "等级": "低", "截止日": ""},
        {"编号": "VF-02", "等级": "中", "截止日": ""},
    ]
    order = [x["编号"] for x in ms_db.sort_items(fake)]
    ck("等级优先：红线在最前", order[0] == "BG-01", str(order[:2]))
    ck("同级有截止日的按日期升序", order[1:3] == ["OP-01", "CX-01"], str(order[1:3]))
    ck("空截止日排在同级最后", order[3:5] == ["AR-02", "AR-10"], str(order[3:5]))
    ck("高 > 中 > 低", order[5:] == ["VF-02", "VF-01"], str(order[5:]))
    ck("编号自然序（AR-02 在 AR-10 前）", order.index("AR-02") < order.index("AR-10"), "")

    # ------------------------------------------------------------ [5] 筛选
    w("")
    w("[5] 筛选 / 搜索 / 分页")
    a = ms_db.add_item(actor="t", path=DB, **{"标题": "筛选甲", "类别": "OP", "项目": "projA",
                                              "状态": "待点名", "备注": "关键字ZEBRA",
                                              "截止日": "2026-06-01"})
    b = ms_db.add_item(actor="t", path=DB, **{"标题": "筛选乙", "类别": "OP", "项目": "projB",
                                              "状态": "已点名"})
    ck("按状态筛", [x["编号"] for x in ms_db.list_items(status="已点名", project="projB", path=DB)]
       == [b["编号"]], "")
    ck("状态多值（英文逗号）",
       len(ms_db.list_items(status="待点名,已点名", path=DB)) >= 2, "")
    ck("状态多值（中文逗号）",
       len(ms_db.list_items(status="待点名，已点名", path=DB)) >= 2, "")
    ck("按项目筛", [x["编号"] for x in ms_db.list_items(project="projA", path=DB)] == [a["编号"]],
       "projA")
    ck("q 搜备注（大小写不敏感）",
       [x["编号"] for x in ms_db.list_items(q="zebra", path=DB)] == [a["编号"]], "zebra")
    ck("q 搜标题", any(x["编号"] == b["编号"] for x in ms_db.list_items(q="筛选乙", path=DB)), "")
    ck("q 搜编号", any(x["编号"] == b["编号"] for x in ms_db.list_items(q=b["编号"], path=DB)), "")
    ck("due_before 排除空截止日",
       all(x["截止日"] for x in ms_db.list_items(due_before="2027-01-01", path=DB))
       and any(x["编号"] == a["编号"] for x in ms_db.list_items(due_before="2027-01-01", path=DB)), "")
    ck("due_before 格式错被拒",
       err_of(ms_db.list_items, due_before="2027/1/1", path=DB) == "bad date", "")
    ck("limit 生效", len(ms_db.list_items(limit=3, path=DB)) == 3, "")
    ck("limit=-1 不限", len(ms_db.list_items(limit=-1, path=DB)) > 5, "")

    # ------------------------------------------------------------ [6] 事件契约
    w("")
    w("[6] 事件 = 审计（同事务 / 无变化不落 / 非法回滚）")
    n0 = n_events()
    it = ms_db.add_item(actor="t", path=DB, **{"标题": "事件契约条", "类别": "CX", "项目": "evt"})
    code = it["编号"]
    ev = all_events()
    ck("add 落一条 add 事件", n_events() == n0 + 1 and ev[-1]["kind"] == "add",
       "kind=%s" % ev[-1]["kind"])
    ck("add 事件带 payload",
       bool(ev[-1]["payload"]) and ev[-1]["payload"].get("status") == "待点名",
       json.dumps(ev[-1]["payload"], ensure_ascii=False)[:70])
    ck("事件记 actor", ev[-1]["actor"] == "t", ev[-1]["actor"])

    n1 = n_events()
    r = ms_db.update_item(code, actor="t", path=DB, **{"等级": "高"})
    # ⚠️ 当前实现回的是**中文**字段名（内部 `COL_FIELDS` 又把英文列名转回去了）。
    #    待办 AR-62 要把它统一成英文（形参也英文），届时这条断言要跟着改。
    ck("update 报 changed（现状：中文名）", r["changed"] == ["等级"], str(r["changed"]))
    ev = all_events()
    ck("update 落一条事件", n_events() == n1 + 1 and ev[-1]["kind"] == "update", ev[-1]["kind"])
    ck("update 事件带 diff（旧→新）",
       (ev[-1]["payload"] or {}).get("diff", {}).get("等级") == ["中", "高"],
       json.dumps((ev[-1]["payload"] or {}).get("diff"), ensure_ascii=False))

    n2 = n_events()
    r = ms_db.update_item(code, actor="t", path=DB, **{"等级": "高"})
    ck("无实际变化 → changed 空", r["changed"] == [], str(r["changed"]))
    ck("无实际变化 → 不落事件", n_events() == n2, "%d → %d" % (n2, n_events()))

    n3 = n_events()
    ck("非法枚举 → bad enum",
       err_of(ms_db.update_item, code, actor="t", path=DB, **{"等级": "乱写"}) == "bad enum", "")
    ck("非法更新 → 事件数不变（同事务回滚）", n_events() == n3, "%d → %d" % (n3, n_events()))
    ck("非法更新 → 字段没被改", ms_db.get_item(code, path=DB)["等级"] == "高",
       ms_db.get_item(code, path=DB)["等级"])
    ck("改编号 → 只读被拒",
       err_of(ms_db.update_item, code, actor="t", path=DB, **{"编号": "AR-99"}) == "field is read-only", "")
    ck("空 fields → nothing to update",
       err_of(ms_db.update_item, code, actor="t", path=DB) == "nothing to update", "")
    ck("改不存在的条目 → not found",
       err_of(ms_db.update_item, "ZZ-99", actor="t", path=DB, **{"等级": "低"}) == "not found", "")

    le = ms_db.list_events(since=0, limit=100000, path=DB)
    ck("last_event 是最新那条", ms_db.last_event(path=DB)["id"] == le["last_id"], str(le["last_id"]))
    ck("since 游标生效",
       all(x["id"] > le["last_id"] - 3
           for x in ms_db.list_events(since=le["last_id"] - 3, path=DB)["events"]), "")
    ck("unread_only 只给未读",
       all(x["ack_at"] == "" for x in ms_db.list_events(unread_only=True, path=DB)["events"]), "")
    ck("事件 limit 生效", len(ms_db.list_events(since=0, limit=2, path=DB)["events"]) == 2, "")

    before_ack = ms_db.list_events(since=0, limit=100000, path=DB)
    unread_a = before_ack["unread"]
    mid = before_ack["events"][-3]["id"]
    exp_acked = len([x for x in before_ack["events"] if x["ack_at"] == "" and x["id"] <= mid])
    r = ms_db.ack_events(up_to=mid, actor="t", path=DB)
    after_ack = ms_db.list_events(since=0, limit=100000, path=DB)
    ck("ack(up_to) 只 ack 该条之前的", r["acked"] == exp_acked,
       "acked=%d 期望=%d" % (r["acked"], exp_acked))
    ck("ack 后 unread 恰好减少 acked 条", after_ack["unread"] == unread_a - exp_acked,
       "unread %d → %d" % (unread_a, after_ack["unread"]))
    ck("后续事件仍是未读", after_ack["events"][-1]["ack_at"] == "", "")
    r = ms_db.ack_events(up_to=0, actor="t", path=DB)
    ck("ack(0) 全部已读", ms_db.list_events(since=0, path=DB)["unread"] == 0, "acked=%d" % r["acked"])

    # ------------------------------------------------------------ [7] 闭环/点名/拍板
    w("")
    w("[7] 闭环 / 点名 / 拍板 语义")
    it = ms_db.add_item(actor="t", path=DB, **{"标题": "流程条", "类别": "AR", "备注": "原有备注"})
    code = it["编号"]
    ms_db.close_item(code, "第一次结论", actor="t", path=DB)
    g = ms_db.get_item(code, path=DB)
    ck("闭环 → 已闭环 + closed_at", g["状态"] == "已闭环" and bool(g["closed_at"]), g["closed_at"])
    ck("闭环不覆盖原备注", g["备注"].startswith("原有备注"), g["备注"][:12])
    ck("结论前缀【闭环 1】", "【闭环 1】第一次结论" in g["备注"], "")
    ms_db.close_item(code, "再来一次", actor="t", path=DB)
    g = ms_db.get_item(code, path=DB)
    ck("二次闭环 → 【闭环 2】且保留 1",
       "【闭环 1】" in g["备注"] and "【闭环 2】" in g["备注"],
       str(re.findall(r"【闭环 \d+】", g["备注"])))
    ck("无结论也能闭环", ms_db.close_item(it["编号"], path=DB)["ok"] is True, "")
    ck("闭环落 close 事件", any(x["kind"] == "close" for x in all_events()), "")

    it = ms_db.add_item(actor="t", path=DB, **{"标题": "点名条", "类别": "AR"})
    r = ms_db.mention_item(it["编号"], "今天做", actor="t", path=DB)
    g = ms_db.get_item(it["编号"], path=DB)
    ck("点名 → 状态已点名", r["状态"] == "已点名" and g["状态"] == "已点名", g["状态"])
    ck("点名写【点名 日期】入备注",
       re.search(r"【点名 \d{4}-\d{2}-\d{2}】今天做", g["备注"]) is not None, g["备注"][:40])
    ck("点名落 mention 事件", any(x["kind"] == "mention" for x in all_events()), "")

    it = ms_db.add_item(actor="t", path=DB, **{"标题": "拍板条", "类别": "AR",
                                               "类型": "待拍板", "状态": "待拍板"})
    r = ms_db.decide_item(it["编号"], "就按 B 走", actor="t", path=DB)
    g = ms_db.get_item(it["编号"], path=DB)
    ck("拍板 → 类型回待办", r["类型"] == "待办" and g["类型"] == "待办", g["类型"])
    ck("拍板 → 待拍板转进行中", g["状态"] == "进行中", g["状态"])
    ck("拍板写【拍板 日期】入备注",
       re.search(r"【拍板 \d{4}-\d{2}-\d{2}】就按 B 走", g["备注"]) is not None, g["备注"][:40])
    ck("拍板落 decide 事件", any(x["kind"] == "decide" for x in all_events()), "")
    ck("空意见拍板被拒", err_of(ms_db.decide_item, it["编号"], "", path=DB) == "empty decision", "")

    it3 = ms_db.add_item(actor="t", path=DB, **{"标题": "已点名的条", "类别": "AR"})
    ms_db.mention_item(it3["编号"], path=DB)
    ms_db.decide_item(it3["编号"], "批了", path=DB)
    ck("已点名的条拍板后仍停在已点名",
       ms_db.get_item(it3["编号"], path=DB)["状态"] == "已点名",
       ms_db.get_item(it3["编号"], path=DB)["状态"])

    ck("close 不存在 → not found", err_of(ms_db.close_item, "ZZ-99", path=DB) == "not found", "")
    ck("mention 不存在 → not found", err_of(ms_db.mention_item, "ZZ-99", path=DB) == "not found", "")
    ck("decide 不存在 → not found",
       err_of(ms_db.decide_item, "ZZ-99", "x", path=DB) == "not found", "")

    # ------------------------------------------------------------ [8] tokens
    w("")
    w("[8] tokens（隐私红线 + 生命周期）")
    t1 = ms_db.add_token("agent-a", role="agent", note="n1", path=DB)
    ck("add_token 明文格式 ms_+48hex",
       re.fullmatch(r"ms_[0-9a-f]{48}", t1["token"] or "") is not None, (t1["token"] or "")[:14] + "…")
    ck("空名被拒", err_of(ms_db.add_token, "", path=DB) == "missing field", "")
    ck("非法角色被拒", err_of(ms_db.add_token, "x", role="root", path=DB) == "bad role", "")

    lst = ms_db.list_tokens(reveal=False, path=DB)
    ck("reveal=False 一个字符都不给", all(x["token"] == "" for x in lst), "共 %d 枚" % len(lst))
    ck("reveal=False 仍给 tail", any(x["tail"] == t1["token"][-6:] for x in lst), t1["token"][-6:])
    ck("reveal=True 含明文",
       any(x["token"] == t1["token"] for x in ms_db.list_tokens(reveal=True, path=DB)), "")

    ck("auth_token 正确 → 带 role",
       (ms_db.auth_token(t1["token"], touch=False, path=DB) or {}).get("role") == "agent", "")
    ck("auth_token 错误 → None", ms_db.auth_token("ms_deadbeef", touch=False, path=DB) is None, "")
    ck("auth_token 空 → None", ms_db.auth_token("", touch=False, path=DB) is None, "")
    ms_db.auth_token(t1["token"], touch=True, path=DB)
    row = [x for x in ms_db.list_tokens(reveal=False, path=DB) if x["id"] == t1["id"]][0]
    ck("touch=True 记 last_used_at", bool(row["last_used_at"]), row["last_used_at"])

    rot = ms_db.rotate_token(t1["id"], actor="t", path=DB)
    ck("rotate 换了值", rot["token"] != t1["token"], rot["token"][:14] + "…")
    ck("rotate 后旧值立即失效", ms_db.auth_token(t1["token"], touch=False, path=DB) is None, "")
    ck("rotate 后新值可用",
       (ms_db.auth_token(rot["token"], touch=False, path=DB) or {}).get("id") == t1["id"], "")

    n4 = n_events()
    v = ms_db.get_token_value(t1["id"], actor="", path=DB)
    ck("取明文无 actor → 不落审计", n_events() == n4 and v["token"] == rot["token"], "")
    ms_db.get_token_value(t1["id"], actor="t", reason="演练", path=DB)
    ev = all_events()
    ck("取明文有 actor → 落审计事件",
       n_events() == n4 + 1 and (ev[-1]["payload"] or {}).get("action") == "reveal",
       json.dumps(ev[-1]["payload"] or {}, ensure_ascii=False)[:60])

    ms_db.revoke_token(t1["id"], actor="t", path=DB)
    ck("吊销后鉴权失败", ms_db.auth_token(rot["token"], touch=False, path=DB) is None, "")
    ck("吊销后取明文被拒", err_of(ms_db.get_token_value, t1["id"], path=DB) == "token revoked", "")
    vr = ms_db.verify_tokens(path=DB)
    ck("verify_tokens 计数自洽", vr["passed"] + vr["failed"] == len(vr["tokens"]),
       "passed=%d failed=%d" % (vr["passed"], vr["failed"]))

    ck("ensure_admin_token 空 token → 不建", ms_db.ensure_admin_token("", path=DB)["ok"] is False, "")
    ea = ms_db.ensure_admin_token("ms_adminfixed", name="迁移", path=DB)
    ck("ensure_admin_token 首次新建 admin",
       ea["ok"] and ea.get("existed") is False
       and (ms_db.auth_token("ms_adminfixed", touch=False, path=DB) or {}).get("role") == "admin", "")
    ck("ensure_admin_token 幂等", ms_db.ensure_admin_token("ms_adminfixed", path=DB).get("existed") is True, "")

    ms_db.mark_pull(t1["id"], path=DB)
    ck("mark_pull 只动 last_pull_at",
       bool([x for x in ms_db.list_tokens(path=DB) if x["id"] == t1["id"]][0]["last_pull_at"]), "")
    ms_db.mark_pull(t1["id"], last_event_id=7, path=DB)
    ck("mark_pull 带游标写 last_event_id",
       [x for x in ms_db.list_tokens(path=DB) if x["id"] == t1["id"]][0]["last_event_id"] == 7, "7")

    # ------------------------------------------------------------ [9] 迁移
    w("")
    w("[9] import_json（v1 → v2 回退路径）")
    jf = os.path.join(TMP, "wb.json")
    payload = [
        {"编号": "AR-01", "标题": "旧条目一", "类型": "待办", "状态": "进行中",
         "等级": "高", "备注": "旧备注一"},
        {"编号": "BG-07", "标题": "旧条目二", "类型": "待办", "状态": "已闭环", "等级": "中"},
        {"标题": "没有编号的脏数据"},
    ]
    with io.open(jf, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False)
    mdb = os.path.join(TMP, "mig.sqlite3")
    m1 = ms_db.import_json(jf, db=mdb)
    ck("导入计数：新增 2 / 覆盖 0", m1["new"] == 2 and m1["updated"] == 0, str(m1))
    ck("无编号脏数据被跳过", ms_db.stats(path=mdb)["total"] == 2,
       "total=%d" % ms_db.stats(path=mdb)["total"])
    ck("已闭环条目补 closed_at", bool(ms_db.get_item("BG-07", path=mdb)["closed_at"]), "")
    ck("导入落 migrate 事件",
       any(x["kind"] == "migrate" for x in ms_db.list_events(since=0, limit=100, path=mdb)["events"]), "")
    ck("meta 记来源", ms_db.meta_get("migrated_from", path=mdb) == jf, "")
    m2 = ms_db.import_json(jf, db=mdb)
    ck("重复导入幂等（覆盖不新增）",
       m2["new"] == 0 and m2["updated"] == 2 and ms_db.stats(path=mdb)["total"] == 2, str(m2))
    bad = os.path.join(TMP, "bad.json")
    with io.open(bad, "w", encoding="utf-8") as f:
        f.write('{"not": "a list"}')
    ck("坏数据文件被拒", err_of(ms_db.import_json, bad, db=mdb) == "bad data file", "")

    # ------------------------------------------------------------ [10] 项目镜像
    w("")
    w("[10] sync_projects（单向镜像导出）")
    root = os.environ["MS_PROJECTS"]
    sdb = os.path.join(TMP, "sync.sqlite3")
    ms_db.init_db(sdb)
    ms_db.add_item(actor="t", path=sdb, **{"标题": "带/非法:字符*的标题", "类别": "AR",
                                           "项目": "proj1", "备注": "备注正文"})
    ms_db.add_item(actor="t", path=sdb, **{"标题": "未归类的条", "类别": "BG"})
    res = ms_db.sync_projects(path=sdb)
    ck("写出两个文件", res["written"] == 2, json.dumps(res["projects"], ensure_ascii=False))
    ck("两个项目目录（空项目落 _未归类）",
       set(res["projects"]) == {"proj1", "_未归类"}, str(list(res["projects"])))
    files = []
    for dp, _d, fs in os.walk(root):
        files += [os.path.join(dp, f) for f in fs]
    ck("文件名净化非法字符",
       all(not re.search(r'[\\/:*?"<>|]', os.path.basename(f)) for f in files),
       str([os.path.basename(f) for f in files]))
    md = [f for f in files if "proj1" in f][0]
    txt = io.open(md, encoding="utf-8").read()
    ck("md 有 YAML 头与字段", txt.startswith("---") and "标题:" in txt and "状态: 待点名" in txt, "")
    ck("md 含备注正文", "备注正文" in txt, "")

    code_s = ms_db.list_items(project="proj1", path=sdb)[0]["编号"]
    ms_db.update_item(code_s, actor="t", path=sdb, **{"标题": "改名了"})
    res2 = ms_db.sync_projects(path=sdb)
    ck("改名后旧文件被清掉", res2["removed"] == 1, "removed=%d" % res2["removed"])
    left = []
    for dp, _d, fs in os.walk(root):
        left += fs
    ck("仍只剩两个文件", len(left) == 2, str(left))

    # ------------------------------------------------------------ [11] stats
    w("")
    w("[11] stats 口径")
    s = ms_db.stats(path=DB)
    items = ms_db.list_items(limit=-1, path=DB)
    ck("total 对得上", s["total"] == len(items), "total=%d" % s["total"])
    ck("open = total − 已闭环/已否决",
       s["open"] == len([x for x in items if x["状态"] not in ms_db.CLOSED]), "open=%d" % s["open"])
    ck("status 分档求和 = total", sum(s["status"].values()) == s["total"],
       json.dumps(s["status"], ensure_ascii=False))
    ck("events_unread 与事件表一致",
       s["events_unread"] == ms_db.list_events(since=0, limit=100000, path=DB)["unread"],
       str(s["events_unread"]))
    ck("tokens 只数未吊销",
       s["tokens"] == len([x for x in ms_db.list_tokens(path=DB) if not x["revoked"]]),
       "tokens=%d" % s["tokens"])

except Exception:                                                 # noqa: BLE001
    import traceback
    w("!!!!! 异常 !!!!!")
    w(traceback.format_exc())
    FAIL += 1
finally:
    real_after = fingerprint(REAL_DB)
    w("")
    w("=" * 68)
    w("[12] 隔离自证：真库指纹前后一致")
    w("  before=%s" % (real_before,))
    w("  after =%s" % (real_after,))
    if real_before == real_after:
        PASS += 1
        w("  [OK]   真库 ms.sqlite3 未被碰")
    else:
        FAIL += 1
        w("  [FAIL] 真库被动过了！")
    w("=" * 68)
    w("结果：PASS %d / FAIL %d" % (PASS, FAIL))
    w("=" * 68)
    io.open(_OUT, "w", encoding="utf-8", newline="\n").write("\n".join(buf) + "\n")
    sys.exit(1 if FAIL else 0)
