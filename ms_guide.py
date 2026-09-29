# -*- coding: utf-8 -*-
"""Mainspring 机枢 · 接入指南（单一真值源）

写成 Python 常量是为了**部署只传一个文件**，同时对外有三个出口，内容永远一致：
    ① MCP 工具  `ms_guide(topic)`      —— 接进来的 agent 自己问
    ② REST      `GET /api/guide`       —— 任何语言的 agent 都能拉
    ③ 网页      「唤醒」页签的「怎么接」区 —— 人看的也是这份

⚠️ 改这份文档 = 改所有出口。别在别处再抄一份。
"""
from __future__ import annotations

import os

#: 文档里统一写这个占位符，渲染时替换成**本实例**的真实基址（见 `_base_url`）。
#: 这样同一份指南在任何部署上都成立，不必改文档内容。
PLACEHOLDER = "__BASE__"


def _base_url() -> str:
    """本实例对外的基址。

    优先 `MS_PUBLIC_URL`（可带路径前缀）；否则用 `MS_PUBLIC_HOST` + `MS_PORT` 拼；
    都没有就退回本机 —— 保证文档里不会出现**别人的**地址。
    """
    u = (os.environ.get("MS_PUBLIC_URL") or "").strip().rstrip("/")
    if u:
        return u
    host = (os.environ.get("MS_PUBLIC_HOST") or "").strip() or "127.0.0.1"
    port = (os.environ.get("MS_PORT") or "8787").strip()
    if ":" in host and not host.startswith("["):     # 裸 IPv6 要加方括号
        host = "[" + host + "]"
    return "http://%s:%s" % (host, port)


def _fill(s: str) -> str:
    return s.replace(PLACEHOLDER, _base_url())


#: 公开别名 —— 服务启动日志等处共用同一个来源，免得日志和指南各说一个地址。
base_url = _base_url


#: 章节键 -> (标题, 正文)
SECTIONS = {}

SECTIONS["intro"] = ("这是什么", """\
Mainspring 机枢 = 工作台所有者的任务看板 + **AI agent 的任务调度入口**。

- 人类用网页看板：`__BASE__`
- AI 用 **REST**（`/api/*`）或 **MCP**（`/mcp`）读写
- 底层是 SQLite（四表：`items` / `events` / `tokens` / `agents`）

**你要做的一件事**：人类在工作台上「点名」某条任务 ⇒ 你拉事件拿到它 ⇒ 干活 ⇒ 回填状态。
""")

SECTIONS["quickstart"] = ("一分钟接上", """\
### 第 0 步：拿 token
人类在工作台「Token」页签给你建一个，角色给 `agent`（能读写任务与事件）。
如果你已经拿到 token，跳过这步。

### 第 1 步：拉未读事件（这就是「接活」）
```bash
curl -H "X-MS-Token: <你的token>" \\
     "__BASE__/api/events?unread=1"
```
返回里每条 `event` 的 `kind`：
| kind | 含义 | 你该做什么 |
|---|---|---|
| `mention` | ★ **人类点名了** | 这是给你的活，去做 |
| `decide` | 人类拍板了 | 按拍板意见执行 |
| `add` / `update` / `close` | 条目增改闭环 | 通常只需知道 |
| `token` / `migrate` | token 变更 / 数据迁移 | 一般忽略 |

### 第 2 步：拿任务全文
事件里只有摘要。全文（含备注）用：
```bash
curl -H "X-MS-Token: <你的token>" \\
     "__BASE__/api/item/CX-05"
```

### 第 3 步：声明「我接了这个活」
```bash
curl -X POST -H "X-MS-Token: <你的token>" -H "Content-Type: application/json" \\
     -d '{"code":"CX-05","状态":"进行中"}' \\
     "__BASE__/api/update"
```
> 状态链：`待点名 → 已点名 → 进行中 → 已闭环`。
> 人类的动作把你推到「已点名」；**你开工后自己改成「进行中」**——这是给人类看的信号。

### 第 4 步：干完回填
```bash
curl -X POST -H "X-MS-Token: <你的token>" -H "Content-Type: application/json" \\
     -d '{"code":"CX-05","conclusion":"实测结论：……"}' \\
     "__BASE__/api/close"
```
`conclusion` 会追加到备注尾部（前缀 `【闭环 N】`）。

### 第 5 步：标记事件已读（**别漏**）
```bash
curl -X POST -H "X-MS-Token: <你的token>" -H "Content-Type: application/json" \\
     -d '{"up_to":17}' "__BASE__/api/events/ack"
```
`up_to` 用上一步拉到的 `last_id`。**不 ack 的话下次还会重复给你同一批。**
""")

SECTIONS["mcp"] = ("走 MCP（更省事）", """\
MCP 通道：`__BASE__/mcp`

客户端配置（**token 拼在 URL 上最通用**，不依赖客户端支不支持自定义 header）：
```json
{
  "mcpServers": {
    "mainspring": {
      "url": "__BASE__/mcp?token=<你的token>"
    }
  }
}
```

工具清单：
| 工具 | 作用 |
|---|---|
| `ms_list` | 列条目（**不含备注**，可筛 status/type/level/project/due_before） |
| `ms_get` | 取单条全字段（含备注全文） |
| `ms_add` | 新增条目。**`category`(AR/BG/CX/OP/VF) 决定编号前缀，各类独立自增**。形参：`title` `category` `type` `level` `project` `location` `note` `due` |
| `ms_update` | 改任意字段（空串 = 不动；`code` 只读）。形参：`title` `type` `status` `level` `project` `location` `due` `note`。例：`ms_update(code="CX-05", status="进行中")` |
| `ms_close` | 闭环 + 写结论 |
| **`ms_events`** | ★ 拉事件流水（`unread_only=True` / `since=<id>`） |
| **`ms_ack`** | ★ 标记已读（`up_to=<last_id>`） |
| **`ms_guide`** | ★ **你正在读的这个**——接入说明 |

推荐开工姿势：
```
ms_events(unread_only=True)   # 人类在我离线期间干了什么
... 逐条处理 ...
ms_ack(up_to=<last_id>)       # 收工标记
```
""")

SECTIONS["api"] = ("REST 速查", """\
鉴权三种写法任选（等价）：
- `X-MS-Token: <token>`
- `Authorization: Bearer <token>`
- `?token=<token>`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 免鉴权探活 |
| GET | `/api/ping` | **token 专用探活**（验 token 有效性就调它） |
| GET | `/api/items?note=0` | 全部条目（`note=0` 去掉备注，省流量） |
| GET | `/api/item/{code}` | 单条全字段 |
| POST | `/api/add` | 新增（body 与 `ms_add` 同） |
| POST | `/api/update` | 改（body: `{"code":"CX-05","状态":"进行中",…}`） |
| POST | `/api/close` | 闭环（body: `{"code":…,"conclusion":…}`） |
| POST | `/api/mention` | 点名（**这是人类的动作，agent 一般不用**） |
| POST | `/api/decide` | 拍板（同上） |
| GET | `/api/events?since=&unread=` | ★ 事件流水（**`limit` 是从 `since` 之后取前 N 条，id 升序** ⇒ 不带 `since` 时拿的是最旧几条，要最新请配 `unread=1`） |
| POST | `/api/events/ack` | ★ 标记已读 |
| GET | `/api/stream` | SSE 长连，实时收事件（`EventSource` 不支持自定义 header ⇒ 用 `?token=`） |
| GET | `/api/stats` | 统计 |
| GET | `/api/tokens/{id}/verify` | 验证某 token 是否可用 —— **不需要明文**（内部走同一条鉴权路径；仅 admin） |
| GET | `/api/tokens/{id}/value` | 取某 token 明文 —— **仅 admin**，**每次取都落一条审计事件**，响应带 `Cache-Control: no-store` |
| GET | `/api/projects` | 项目文件夹树 |
| GET | `/api/projects/file?path=` | 读某个 .md 正文 |
| GET | `/api/guide` | ★ 本指南（JSON：`{sections:[{key,title,md}]}`） |

角色权限：`readonly` 只能 GET；`agent` 可读写任务与事件；`admin` 额外能管 token 与唤醒钩子。
""")

SECTIONS["hook"] = ("被主动唤醒（可选）", """\
默认模式是**你主动来拉**。如果你能起一个 HTTP 服务，还可以让工作台**点名时直接推给你**。

1. 人类在「唤醒」页签新建一个目标，填你的地址，例如 `http://<你的接收端>:9010/wake`
2. 点名时，工作台向该地址 POST：
```json
{
  "event": "mention",
  "code": "CX-05",
  "title": "…",
  "opinion": "点名人的附言",
  "status": "已点名",
  "project": "demo",
  "level": "高",
  "actor": "human",
  "ts": "2026-09-20T18:33:14",
  "dashboard": "__BASE__/",
  "howto": "带你的 token 调 GET /api/item/CX-05 拿备注全文；跑起来后把状态改成「进行中」，跑完 /api/close。"
}
```
3. 请求头带 `X-MS-Event`（配了 token 则再加 `X-MS-Token` + `Authorization`）
4. 你回 2xx 即算成功；工作台会在「唤醒」页显示 `ok:200` 或失败原因

⚠️ **失败不影响点名本身**：工作台先落库落事件，再推你；推失败只记 `last_status`，超时 5 秒。
⚠️ 推送是**直连不走代理**。如果你那边收不到，先查是不是 `http_proxy` 在捣乱。
""")

SECTIONS["conventions"] = ("约定与坑", """\
### 任务约定
- **状态链**：`待点名 → 已点名 → 进行中 → 已闭环 / 已否决`
  · 人类点名 → `已点名`；你开工 → 自己改 `进行中`；干完 → `/api/close`
- **改状态用 `/api/update`**，闭环用 `/api/close`（后者会追加备注段）
- **不要自己改「编号」**（只读主键）

### 编号体系（别猜）
`AR` 架构 · `BG` Bug · `CX` 实验探讨 · `OP` 运维流程 · `VF` 验证记录。
**各类独立自增**，新增时用 `类别` 参数指定前缀。

### 🔴 三个坑
1. **Cookie 优先于 token**：如果你同时带了工作台的登录 Cookie，会被当成"人类"，
   于是 **token 的拉活痕迹（`last_pull_at` / `last_event_id`）不会记录**。
   ⇒ **agent 只带 token，别带 Cookie。**
2. **`/api/update` 会过滤空串** ⇒ 想清空某字段做不到（历史遗留）。
3. **不 ack 就会重复收到同一批事件**。拉完记得 `ms_ack(up_to=last_id)`。

### 参数名：**MCP 工具的形参是英文**（2026-09-20 起 · BG-28）
对应的工作台字段是中文 ——
`status`=状态 · `title`=标题 · `note`=备注 · `level`=等级 ·
`project`=项目 · `location`=位置 · `type`=类型 · `due`=截止日 · `category`=类别（仅 ms_add）。
⚠️ **别用中文当关键字传参** —— 某些 MCP 客户端会把中文标识符**转义成 `___N`**，
导致接进来的 agent 只能靠猜参数名（**BG-28 就是这么被发现的**）。

### token 的隐私约定
- **任何列表接口都不返回明文**：`GET /api/tokens` 只给 `tail`（尾号），明文字段恒为空串。
- 要明文只能 `GET /api/tokens/{id}/value` 单取一次，**每次取都记审计事件**（谁、何时、哪个 token）。
- **你不需要明文**：你的 token 只在请求头里用（`X-MS-Token`），不必去看它长什么样。

### 项目文件夹（只读镜像）
工作台会把每条任务镜像到 `projects/<项目>/<编号>-<标题>.md`。
⚠️ **这是单向导出**：SQLite 是唯一真值源，直接改文件会被覆盖。
""")

SECTIONS["tasks"] = ("人类交给你的活，怎么做", """\
1. **开工先拉**：`ms_events(unread_only=True)` —— 看人类在你离线期间点了哪些名
2. **拿全文再动手**：`ms_get(code)` —— 事件摘要不够，备注里才有完整上下文
3. **声明接手**：`ms_update(code="CX-05", status="进行中")` —— 人类在网页上能看到你动了
4. **干活**（这步是你的本职）
5. **闭环必须留结论**：`ms_close(code, conclusion="…")`
   · 结论写给**未来的负责人和未来的你**：做了什么、实测到什么、**遗留什么**
   · ⚠️ **「已闭环」= 收口，不等于「修好了」** —— 没真改的事，结论里要写明
6. **收工 ack**：`ms_ack(up_to=<last_id>)`
7. **有发现要单独说**：不要把"顺带发现"混进本次交付的结论里，另开一条（本项目的明确要求）
""")

ORDER = ["intro", "quickstart", "mcp", "api", "hook", "conventions", "tasks"]


def render(topic: str = "") -> str:
    """渲染整篇或单节。topic 为空 ⇒ 全篇（含目录）。"""
    t = (topic or "").strip().lower()
    if t and t in SECTIONS:
        return _fill("# %s\n\n%s" % (SECTIONS[t][0], SECTIONS[t][1].strip()))
    if t:
        keys = [k for k in ORDER if t in k or t in SECTIONS[k][0]]
        if len(keys) == 1:
            return render(keys[0])
    parts = ["# Mainspring 机枢 · 接入指南", "",
             "> 面向接入工作台的 AI agent。可整篇读，也可按主题查。", ""]
    parts.append("## 目录")
    for k in ORDER:
        parts.append("- `%s` — %s" % (k, SECTIONS[k][0]))
    parts.append("")
    for k in ORDER:
        parts.append(SECTIONS[k][1].strip())
        parts.append("")
        parts.append("---")
        parts.append("")
    return _fill("\n".join(parts).rstrip()) + "\n"


def as_json() -> dict:
    return {"ok": True,
            "sections": [{"key": k, "title": SECTIONS[k][0], "md": _fill(SECTIONS[k][1].strip())}
                         for k in ORDER],
            "full_md": render()}
