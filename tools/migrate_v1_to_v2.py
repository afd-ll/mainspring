# -*- coding: utf-8 -*-
"""v1(wb.json) -> v2(ms.sqlite3) 迁移 + 逐项校验

在**服务器上**跑（真值源在服务器）：
    venv/bin/python tools/migrate_v1_to_v2.py        # 在仓库根跑

幂等：重复跑按「编号」覆盖，不会重复插入。**wb.json 原地保留，不删、不改**（回退要用）。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 本脚本在 tools/ 下，仓库根是上一级
sys.path.insert(0, ROOT)

import ms_db  # noqa: E402

JSON_PATH = os.environ.get("MS_JSON") or os.path.join(ROOT, "wb.json")
DB_PATH = os.environ.get("MS_DB") or os.path.join(ROOT, "ms.sqlite3")
FIELDS = ("标题", "类型", "状态", "等级", "项目", "位置", "父", "联动", "截止日")


def main() -> int:
    print("=" * 62)
    print("v1 -> v2 迁移")
    print("=" * 62)
    if not os.path.exists(JSON_PATH):
        print("FAIL 找不到 %s" % JSON_PATH)
        return 1

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        src = json.load(f)
    if not isinstance(src, list):
        print("FAIL %s 根节点不是数组" % JSON_PATH)
        return 1
    print("源   %s  %d 条" % (JSON_PATH, len(src)))
    print("目标 %s" % DB_PATH)

    ms_db.init_db(DB_PATH)
    r = ms_db.import_json(JSON_PATH, actor="migrate", db=DB_PATH)
    print("导入 total=%d new=%d updated=%d" % (r["total"], r["new"], r["updated"]))
    print("")

    # ---------------- 校验 ----------------
    print("校验")
    mine = ms_db.list_items(limit=-1, path=DB_PATH)
    a = {str(x.get("编号") or ""): x for x in src}
    b = {str(x.get("编号") or ""): x for x in mine}
    ok = True

    def ck(name, cond, detail=""):
        nonlocal ok
        print("  [%s] %-28s %s" % ("OK" if cond else "FAIL", name, detail))
        if not cond:
            ok = False

    ck("条数", len(a) == len(b), "json=%d sqlite=%d" % (len(a), len(b)))
    ck("编号集合", set(a) == set(b),
       "缺=%s 多=%s" % (sorted(set(a) - set(b))[:5], sorted(set(b) - set(a))[:5]))

    bad = []
    for code, x in a.items():
        y = b.get(code)
        if not y:
            continue
        for f in FIELDS:
            if str(x.get(f) or "") != str(y.get(f) or ""):
                bad.append("%s.%s: json=%r sqlite=%r" % (code, f, x.get(f), y.get(f)))
    ck("字段逐条一致（%d 字段 x %d 条）" % (len(FIELDS), len(a)), not bad,
       "" if not bad else "差异 %d 处 %s" % (len(bad), bad[:3]))

    ta = sum(len(str(x.get("备注") or "")) for x in a.values())
    tb = sum(len(str(y.get("备注") or "")) for y in b.values())
    ck("备注总字符数", ta == tb, "json=%d sqlite=%d" % (ta, tb))

    nd = sum(1 for c in a if str(a[c].get("备注") or "") != str(b.get(c, {}).get("备注") or ""))
    ck("备注逐条逐字一致", nd == 0, "不一致 %d 条" % nd)

    # ---------------- 旧 token 落库（保证现有 agent 连接不断） ----------------
    tok = os.environ.get("MS_TOKEN", "").strip()
    if tok:
        rr = ms_db.ensure_admin_token(tok, name="v1-迁移(MS_TOKEN)", path=DB_PATH)
        print("  [OK] %-28s %s" % ("旧 MS_TOKEN 落库", rr))
    else:
        print("  [--] %-28s 环境变量 MS_TOKEN 为空，跳过" % "旧 MS_TOKEN 落库")

    st = ms_db.stats(path=DB_PATH)
    print("")
    print("统计 total=%d open=%d events=%d tokens=%d" %
          (st["total"], st["open"], st["events"], st["tokens"]))
    print("")
    print("=" * 62)
    print("迁移%s" % ("成功 ✅" if ok else "失败 ❌"))
    print("=" * 62)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
