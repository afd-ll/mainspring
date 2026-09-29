# -*- coding: utf-8 -*-
"""Mainspring 机枢 v2 · MCP 注册层

把 `ms_db` 暴露成 MCP 工具。返回一律 **JSON 字符串**（便于 agent 解析）。

v1 的五个工具**签名原样保留**（ms_list / ms_get / ms_add / ms_update / ms_close）
—— 已经有 agent 在用，改签名就是断供。

v2 新增两个（**"老大点名/拍板 → AI 同步收到" 的消费入口**）：
    ms_events(since, unread_only, limit)   增量拉事件流水
    ms_ack(up_to)                          标记已读

典型开工姿势：
    ms_events(unread_only=True)   -> 看老大在我离线期间干了什么
    ... 处理 ...
    ms_ack(up_to=<最大 id>)       -> 标记已读，下次不再重复给我

注入用的 MCP server 实例由调用方传入（本模块不自己 new，便于挂 HTTP 传输）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import ms_db  # noqa: E402
import ms_guide as _guide  # noqa: E402  ⚠️ 必须起别名：下面有个工具函数也叫 ms_guide，会遮蔽模块名

mcp = FastMCP("mainspring")


def _ok(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _guard(fn, /, **kw) -> str:
    try:
        return _ok(fn(**kw))
    except ms_db.MsError as e:
        return _ok(e.payload)
    except Exception as e:  # noqa: BLE001
        return _ok({"ok": False, "error": "internal", "detail": "%s: %s" % (type(e).__name__, e)})


# ---------------------------------------------------------------- v1 兼容五件套
@mcp.tool()
def ms_list(
    status: str = "",
    type: str = "",
    level: str = "",
    due_before: str = "",
    project: str = "",
    limit: int = 50,
) -> str:
    """列出工作台条目（**不含备注**，太长）。

    status / type / level / project 支持逗号分隔多值，如 "待点名,待拍板"。
    due_before 取截止日早于该日期（YYYY-MM-DD）的条目（空截止日不算）。
    排序：等级（红线>高>中>低）→ 截止日（空的最后）→ 编号。
    """
    items = ms_db.list_items(status=status, type=type, level=level, project=project,
                             due_before=due_before, limit=-1)
    if limit is not None and int(limit) >= 0:
        items = items[: int(limit)]
    return _ok([{k: v for k, v in it.items() if k != "备注"} for it in items])


@mcp.tool()
def ms_get(code: str) -> str:
    """取单条全字段（含备注全文）。code 形如 "AR-20" 或 "BG-09"。找不到 ⇒ {"error":"not found"}"""
    it = ms_db.get_item(code)
    if not it:
        return _ok({"error": "not found", "code": code})
    return _ok(it)


@mcp.tool()
def ms_add(
    title: str,                     # 标题
    category: str = "",             # 类别 ∈ AR/BG/CX/OP/VF，决定编号前缀
    type: str = "",                 # 类型
    level: str = "",                # 等级
    project: str = "",              # 项目
    location: str = "",             # 位置
    note: str = "",                 # 备注
    due: str = "",                  # 截止日
) -> str:
    """新增一条。

    🔴 **形参是英文，对应的工作台字段见下**（2026-09-20 由中文形参改来，见 BG-28）：
        title→标题 · category→类别 · type→类型 · level→等级
        project→项目 · location→位置 · note→备注 · due→截止日

    `category` ∈ AR / BG / CX / OP / VF ⇒ **决定编号前缀，各类独立自增**（如 category=BG ⇒ BG-26）。
    不给则默认 AR。默认：type=待办 · level=中 · status=待点名。
    ⚠️ 要落在某个「项目」上的条目，**这一次就要带上 project**（事后 update 改项目见 ms_update）。
    """
    fields = {"标题": title, "类型": type or "待办", "等级": level or "中",
              "项目": project, "位置": location, "备注": note, "截止日": due}
    if category:
        fields["类别"] = category
    return _guard(ms_db.add_item, actor="mcp", **fields)


@mcp.tool()
def ms_update(
    code: str,
    title: str = "",                # 标题
    type: str = "",                 # 类型
    status: str = "",               # 状态
    level: str = "",                # 等级
    project: str = "",              # 项目
    location: str = "",             # 位置
    due: str = "",                  # 截止日
    note: str = "",                 # 备注
) -> str:
    """改任意字段（只传要改的；空串视为"不动"）。编号为只读主键，改它会报错。

    🔴 **形参是英文，对应的工作台字段见下**（2026-09-20 由中文形参改来，见 BG-28）：
        title→标题 · type→类型 · status→状态 · level→等级
        project→项目 · location→位置 · due→截止日 · note→备注

    常用：`ms_update(code="CX-06", status="进行中")` ← agent 接手任务时该做的第一件事。
    ⚠️ 实测可改 = 标题 / 状态 / 等级 / 位置 / 截止日 / 备注 / 项目 / 类型。
    """
    fields = {"标题": title, "类型": type, "状态": status, "等级": level,
              "项目": project, "位置": location, "截止日": due, "备注": note}
    return _guard(ms_db.update_item, code=code, actor="mcp",
                  **{k: v for k, v in fields.items() if v})


@mcp.tool()
def ms_close(code: str, conclusion: str = "") -> str:
    """闭环：状态 → 已闭环；给了 conclusion 就追加到备注尾部（前缀 【闭环 N】）。"""
    return _guard(ms_db.close_item, code=code, conclusion=conclusion or None, actor="mcp")


# ---------------------------------------------------------------- ★ v2 新增
@mcp.tool()
def ms_events(since: int = 0, unread_only: bool = False, limit: int = 100) -> str:
    """★ 拉工作台事件流水 —— **老大点名 / 拍板 / 新增 / 改动 / 闭环都在这里**。

    开工前先调一次 `ms_events(unread_only=True)`，就知道你离线期间老大做了什么。
    想只看某条之后的：传 `since=<上次拿到的 last_id>`。
    返回 {ok, events:[{id,ts,actor,kind,code,title,summary,payload,ack_at}], last_id, unread}。
    `kind` = add / update / close / **mention（点名）** / **decide（拍板）** / token / migrate。
    消费完记得调 ms_ack(up_to=last_id)，否则下次还会重复给你。
    """
    return _guard(ms_db.list_events, since=since, unread_only=unread_only, limit=limit)


@mcp.tool()
def ms_ack(up_to: int = 0) -> str:
    """★ 把 <= up_to 的未读事件标记已读（up_to=0 ⇒ 全部标记）。参数一般用 ms_events 返回的 last_id。"""
    return _guard(ms_db.ack_events, up_to=up_to, actor="mcp")


@mcp.tool()
def ms_guide(topic: str = "") -> str:
    """★ 工作台接入指南 —— **第一次接进来先读这个**（和网页上「唤醒」页里那份是同一份）。

    留空 ⇒ 整篇（含目录）。也可按主题取单节：
        intro        这是什么
        quickstart   一分钟接上（拿 token → 拉事件 → 声明接手 → 闭环 → ack）
        mcp          MCP 工具清单 + 推荐开工姿势
        api          REST 速查表 + 三种鉴权写法
        hook         被主动唤醒（工作台点名时 POST 给你的载荷格式与约定）
        conventions  状态链 / 编号体系 / **三个坑**
        tasks        老大交给你的活该怎么做（含「已闭环 ≠ 修好了」）

    返回 Markdown 文本（不是 JSON），直接读即可。
    """
    return _guide.render(topic)


if __name__ == "__main__":
    mcp.run()  # stdio：读本机 ms.sqlite3（注意真值源在服务器，本地跑仅供离线调试）
