#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一个**中性**的演示数据库 —— 用来截图、给第一次跑起来的人看。

⚠️ 只写 --db 指定的库（默认 <仓库根>/demo.sqlite3），**绝不碰 ms.sqlite3**。
   脚本对此有硬检查：目标路径一旦等于真实库就拒绝运行。

用法
    venv/bin/python tools/seed_demo.py                        # → ./demo.sqlite3
    venv/bin/python tools/seed_demo.py --reset                # 先删掉再建
    venv/bin/python tools/seed_demo.py --projects ./projects  # 顺带生成项目文件夹镜像

演示数据讲的是虚构项目「notecli —— 一个命令行笔记工具」的日常，覆盖：
    5 个编号前缀 · 4 种等级 · 6 种状态 · 3 种类型
    新增 / 改字段 / 点名 / 拍板 / 闭环 各若干条事件
    一条带完整时间线的长备注（用来截详情弹窗）
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ms_db  # noqa: E402

REAL_DB = os.path.abspath(os.path.join(ROOT, "ms.sqlite3"))

# (类别, 标题, 类型, 等级, 项目, 位置, 截止日)
ITEMS = [
    ("BG", "`notecli sync` 在文件被外部改动时会丢掉最后一段",
     "待办", "红线", "notecli", "src/sync.rs:210（diff 合并）", "2026-03-04"),
    ("BG", "大笔记（>1MB）打开要 4 秒以上",
     "待办", "高", "notecli", "src/render.rs", ""),
    ("BG", "`--tag` 遇到中文标签会按字节截断",
     "待办", "中", "notecli", "src/tag.rs:88", ""),
    ("AR", "索引：换成 SQLite FTS5，还是自己写倒排",
     "待拍板", "高", "notecli", "设计文档 §3", "2026-03-10"),
    ("AR", "插件机制：进程内 dlopen，还是起子进程走 JSON-RPC",
     "待办", "高", "notecli", "", ""),
    ("AR", "配置格式从 TOML 换成 YAML",
     "待办", "中", "notecli", "config.rs", ""),
    ("CX", "试着用 ripgrep 当搜索引擎，而不是自己扫目录",
     "待办", "中", "notecli", "", ""),
    ("OP", "发布流程：打 tag 自动出 artifact",
     "待办", "中", "notecli", ".github/workflows/release.yml", ""),
    ("OP", "CI 上加 macOS 构建",
     "待办", "低", "notecli", ".github/workflows/ci.yml", ""),
    ("VF", "实测 10 万条笔记的搜索延迟",
     "验证记录", "中", "notecli", "bench/search_latency.md", ""),
    ("AR", "文档站搜索要不要接 Algolia",
     "待拍板", "中", "docs-site", "docs/search.md", ""),
    ("BG", "移动端代码块横向溢出，撑破版面",
     "待办", "低", "docs-site", "docs/theme.css", ""),
    ("OP", "文档站每晚构建 + 部署到 Pages",
     "待办", "中", "docs-site", ".github/workflows/docs.yml", ""),
    ("VF", "Lighthouse 基线分数（移动端 / 桌面端）",
     "验证记录", "低", "docs-site", "bench/lighthouse.md", ""),
    # 下面这些不推进状态，纯粹让板子看起来像在真用
    ("AR", "数据库 schema 版本化与迁移路径",
     "待办", "中", "notecli", "src/db.rs", ""),
    ("BG", "`--regex` 在 Windows 上路径分隔符不对",
     "待办", "低", "notecli", "src/query.rs", ""),
    ("CX", "试试把解析器编成 wasm，跑在浏览器里",
     "待办", "低", "notecli", "", ""),
    ("OP", "加一个 `notecli doctor` 自检命令",
     "待办", "中", "notecli", "", ""),
    ("VF", "跨文件系统测 rename 的原子性",
     "验证记录", "中", "notecli", "bench/rename_atomic.md", ""),
    ("AR", "文档站要不要做多语言",
     "待拍板", "中", "docs-site", "", ""),
]

# 引用方式：以 ITEMS 里的**下标**为键。
# ⚠️ 别用编号当键 —— 编号是按类别独立自增的，按全局顺序推算必错。
FLOW = {
    0: [("close", "定位到 `sync.rs:210` 的合并循环：外部改动时 `tail` 变量没更新，"
                  "最后一次 flush 写的是旧快照。\n\n"
                  "改法：合并前先按 mtime 重新读一遍，并把 tail 指向最新偏移。\n\n"
                  "实测：构造 200 次外部改动 + 并发 sync，修复前丢 37 次，修复后 0 次。\n\n"
                  "遗留：Windows 上 mtime 精度是 2 秒，同一秒内的两次改动仍可能被合并 —— "
                  "已另开一条跟踪。")],
    1: [("update", {"状态": "进行中"}),
        ("update", {"备注": "确认瓶颈不在渲染，是每次打开都把整篇重新分词。\n\n"
                            "下一步：把分词结果缓存到 `~/.cache/notecli/`，"
                            "以文件 mtime + size 为 key。"})],
    2: [("close", "根因是 `Chars::take(n)` 按字节取。\n\n"
                  "改成按 char 边界回溯，并对超长标签给一条 WARN 而不是静默截断。\n\n"
                  "实测：200 个随机中文标签全部完整保留。")],
    3: [("decide", "先上 FTS5。理由：单文件、零依赖、跟着 SQLite 一起走，"
                   "现在的库已经够小；自己写倒排至少要两周，收益只有中文分词这一项。\n\n"
                   "中文分词留成插件点，等真有需求再说。"),
        ("update", {"截止日": "2026-03-20"})],
    4: [("mention", "这条是这轮重点，先做设计再动手。注意插件崩了不能把主进程带走。")],
    5: [("update", {"状态": "已否决"}),
        ("update", {"备注": "否决理由：TOML 能表达的东西够用了，"
                            "换格式除了让所有人重写一遍配置没有别的好处。\n\n"
                            "真要改进，不如把配置项的错误提示做清楚。"})],
    6: [("close", "结论：ripgrep 的 `--json` 输出确实好用，冷启动也比自己扫目录快。\n\n"
                  "但它意味着多一个外部二进制依赖，跨平台分发要各带一份 —— "
                  "对一个小工具来说不划算。**不做**。\n\n"
                  "⚠️ 这条是「已闭环」但结论是放弃：闭环 = 收口，不等于改好了。")],
    7: [("close", "已上：打 `v*` tag 触发 workflow，产出 linux / macos 两个 artifact。\n\n"
                  "实测用 `v0.3.0-rc1` 跑通，两端都能下载运行。")],
    8: [("mention", "先在免费额度用完之前试一次，不行就砍掉。")],
    9: [("close", "实测环境：M2 MacBook Air / APFS。\n\n"
                  "| 笔记数 | 冷启动首次搜索 | 热搜索 |\n|---|---|---|\n"
                  "| 1 万 | 120ms | 18ms |\n| 10 万 | 890ms | 45ms |\n\n"
                  "结论：10 万条以内可用。冷启动那 890ms 主要是建索引，"
                  "可以后台预热 —— 已开一条跟踪。")],
    10: [("decide", "不接。文档就那么点内容，自带搜索够用了。"
                    "等哪天搜索日志里真出现高频 miss 再谈。")],
    11: [("update", {"状态": "进行中"})],
    12: [("close", "每晚 04:30 构建 + 部署。\n\n"
                   "顺手加了失败时往仓库 issue 发通知 —— 沉默的部署失败比没有部署更糟。")],
    13: [("close", "移动端 62 / 桌面端 97。\n\n"
                   "移动端的扣分全在首屏图片，已改成 `loading=lazy`，下轮再看。")],
    # 「待拍板」是这个项目的核心卖点，演示数据里必须有一条真的卡在这里
    19: [("update", {"状态": "待拍板"})],
}


def _seed(db: str) -> None:
    ms_db.init_db(db)
    codes = []
    for cat, title, typ, level, proj, loc, due in ITEMS:
        fields = {"标题": title, "类别": cat, "类型": typ, "等级": level, "项目": proj}
        if loc:
            fields["位置"] = loc
        if due:
            fields["截止日"] = due
        codes.append(ms_db.add_item(actor="you", path=db, **fields)["编号"])

    for idx, steps in FLOW.items():
        code = codes[idx]
        for act, arg in steps:
            if act == "close":
                ms_db.close_item(code, arg, actor="agent", path=db)
            elif act == "mention":
                ms_db.mention_item(code, arg, actor="you", path=db)
            elif act == "decide":
                ms_db.decide_item(code, arg, actor="you", path=db)
            elif act == "update":
                ms_db.update_item(code, actor="agent", path=db, **arg)

    # 一条"被点名、agent 已接手、还没干完"的
    ms_db.update_item(codes[4], actor="agent", path=db, **{"状态": "进行中"})

    ms_db.add_token("demo-agent", role="agent",
                    note="演示用；真实部署请自己在「Token」页签建", actor="you", path=db)
    ms_db.add_agent("my-agent", hook_url="http://127.0.0.1:9010/wake",
                    note="演示用唤醒目标", actor="you", path=db)


def _spread_timeline(db: str) -> None:
    """把时间铺开 —— 否则整屏都是同一秒，既假又难读。"""
    with sqlite3.connect(db) as c:
        rows = [r[0] for r in c.execute("SELECT id FROM events ORDER BY id ASC").fetchall()]
        if not rows:
            return
        base = datetime.datetime.now().replace(microsecond=0)
        n = len(rows)
        for i, eid in enumerate(rows):
            ts = base - datetime.timedelta(minutes=23 * (n - i))
            c.execute("UPDATE events SET ts=? WHERE id=?", (ts.strftime("%Y-%m-%d %H:%M:%S"), eid))
        c.execute("UPDATE items SET created_at=(SELECT min(ts) FROM events WHERE events.code=items.code)")
        c.execute("UPDATE items SET updated_at=(SELECT max(ts) FROM events WHERE events.code=items.code)")
        c.execute("UPDATE items SET closed_at=(SELECT max(ts) FROM events WHERE events.code=items.code "
                  "AND kind='close') WHERE status='已闭环'")
        c.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成中性演示数据库")
    ap.add_argument("--db", default=os.path.join(ROOT, "demo.sqlite3"))
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--projects", default="", help="把项目文件夹镜像生成到这个目录")
    a = ap.parse_args()

    db = os.path.abspath(a.db)
    if db == REAL_DB or db.endswith("ms.sqlite3"):
        print("拒绝运行：目标像是真实数据库（%s）。演示库请用 demo.sqlite3 这类名字。"
              % db, file=sys.stderr)
        return 2

    if a.reset:
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(db + suf)
            except OSError:
                pass

    print("生成演示库 -> %s" % db)
    _seed(db)
    _spread_timeline(db)

    st = ms_db.stats(path=db)
    print("  条目 %d · 事件 %d · token %d · 唤醒目标 %d"
          % (st["total"], st["events"], st["tokens"], len(ms_db.list_agents(path=db))))
    print("  项目：" + " · ".join("%s(%d)" % (k, v) for k, v in st["project"].items()))
    print("  状态：" + " · ".join("%s(%d)" % (k, v) for k, v in st["status"].items()))

    if a.projects:
        old = os.environ.get("MS_PROJECTS")
        os.environ["MS_PROJECTS"] = os.path.abspath(a.projects)
        r = ms_db.sync_projects(path=db)
        if old is None:
            os.environ.pop("MS_PROJECTS", None)
        else:
            os.environ["MS_PROJECTS"] = old
        print("  项目镜像 -> %s（%d 个 .md）" % (r["root"], r["written"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
