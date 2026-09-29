# -*- coding: utf-8 -*-
"""Mainspring 机枢 v2 · REST API 层（人类通道 + AI 通道共用）

鉴权（两条路，按序尝试）：
    ① Cookie  `ms_session`（浏览器登录后签发，HMAC-SHA256，7 天）
    ② Token   `Authorization: Bearer` / `X-MS-Token` / `?token=`（多 token，库中管理）

v1 兼容（签名与返回结构不变，别改）：
    GET  /api/items   -> {count, items}
    GET  /api/build   -> {id}
    POST /api/update  {code, ...字段}      -> {ok, changed}
    POST /api/add     {标题, 类别, ...}     -> {ok, 编号}
    POST /api/close   {code, conclusion}   -> {ok, 编号}

v2 新增：
    ★ POST /api/mention          点名（状态→进行中 + mention 事件）
    ★ POST /api/decide           拍板（备注 + 类型→待办 + decide 事件）
    ★ GET  /api/events           增量拉事件（?since= / ?unread=1）
    ★ POST /api/events/ack       标记已读
    ★ GET  /api/stream           SSE 实时推送
      GET  /api/ping             token 专用探活（前端「验证」按钮打它）
      GET/POST /api/tokens       列表 / 新建
      POST /api/tokens/{id}/rotate  替换（旧值立即失效）
      POST /api/tokens/{id}/revoke  吊销
      GET  /api/tokens/verify    全量自检
      GET  /api/projects         项目文件夹树
      GET  /api/projects/file    读单个 md（防目录穿越）
      GET  /api/stats  /api/whoami  /api/logout
    ★ GET/POST /api/settings     实例设置（界面语言 / 主题 / 看板显示 / 安全）
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time

from starlette.responses import JSONResponse, Response

import ms_db
import ms_guide
import ms_settings
from ms_db import MsError

try:
    from sse_starlette.sse import EventSourceResponse
except ImportError:                       # 本机没装时降级（SSE 路由返回 501）
    EventSourceResponse = None

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PROJECTS_ROOT = os.path.join(HERE, "projects")
INDEX_NAME = "index.html"

USER = os.environ.get("MS_USER", "admin").strip()
PASS = os.environ.get("MS_PASS", "").strip()
SECRET = (os.environ.get("MS_SECRET", "") or ("ms::" + PASS)).encode()
PUBLIC_HOST = os.environ.get("MS_PUBLIC_HOST", "127.0.0.1").strip()
PORT = os.environ.get("MS_PORT", "8787").strip()
TTL = 7 * 86400
COOKIE = "ms_session"

_ROLE_RANK = {"readonly": 1, "agent": 2, "admin": 3}
#: 请求要求的权限等级 -> 需要的最低角色分。
#: ⚠️ 必须与 _ROLE_RANK 分开：read 不是一种角色，它是"只读就够"这个要求。
_NEED_RANK = {"read": 1, "agent": 2, "admin": 3}


# ---------------------------------------------------------------- Cookie
def _sign(payload: str) -> str:
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_cookie(user: str) -> str:
    payload = "%s|%d" % (user, int(time.time()) + TTL)
    return base64.urlsafe_b64encode(("%s|%s" % (payload, _sign(payload))).encode()).decode()


def check_cookie(val: str):
    if not val:
        return None
    try:
        user, exp, sig = base64.urlsafe_b64decode(val.encode()).decode().rsplit("|", 2)
        if _sign("%s|%s" % (user, exp)) != sig:
            return None
        if int(exp) < time.time():
            return None
        return user
    except Exception:
        return None


# ---------------------------------------------------------------- 鉴权
def extract_token(request) -> str:
    t = request.headers.get("x-ms-token", "") or ""
    if not t:
        a = request.headers.get("authorization", "") or ""
        if a[:7].lower() == "bearer ":
            t = a[7:].strip()
    if not t:
        t = request.query_params.get("token", "") or ""
    return t.strip()


def authenticate(request):
    """返回 {'who','role','via'} 或 None。"""
    u = check_cookie(request.cookies.get(COOKIE, ""))
    if u:
        return {"who": u, "role": "admin", "via": "cookie"}
    row = ms_db.auth_token(extract_token(request))
    if row:
        return {"who": row["name"], "role": row["role"], "via": "token", "token_id": row["id"]}
    return None


def _need(request, level: str = "agent"):
    """level: read / agent / admin。放行返回 None，否则返回 JSONResponse。"""
    me = authenticate(request)
    if not me:
        return None, JSONResponse(
            {"ok": False, "error": "unauthorized",
             "hint": "浏览器请 /login；脚本请带 X-MS-Token / Authorization: Bearer / ?token="},
            status_code=401)
    if _ROLE_RANK.get(me["role"], 0) < _NEED_RANK.get(level, 2):
        return None, JSONResponse(
            {"ok": False, "error": "forbidden", "role": me["role"], "need": level},
            status_code=403)
    return me, None


def _err(e: MsError):
    return JSONResponse(e.payload, status_code=400)


# ---------------------------------------------------------------- 事件总线（SSE）
class Bus:
    """进程内广播：写操作 -> publish -> 所有 SSE 订阅者立刻收到。"""

    def __init__(self):
        self.loop = None
        self.subs = set()

    def bind(self, loop):
        self.loop = loop

    def publish(self, ev: dict):
        if self.loop is None:
            return
        for q in list(self.subs):
            try:
                self.loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception:
                pass

    def emit_latest(self, kind: str = ""):
        """写完成后广播「有变化」——只推信号 + 摘要，数据仍走 REST 增量拉，避免两套真值。"""
        try:
            ev = ms_db.last_event()
        except Exception:
            ev = {}
        self.publish({
            "kind": ev.get("kind") or kind,
            "code": ev.get("code", ""),
            "title": ev.get("title", ""),
            "summary": ev.get("summary", ""),
            "last_id": ev.get("id", 0),
            "ts": ev.get("ts", ""),
        })


BUS = Bus()


def _after_write(kind: str):
    """写操作的统一收尾：同步项目文件夹镜像 + 广播「有变化」信号。"""
    try:
        ms_db.sync_projects()
    except Exception as e:                      # noqa: BLE001
        print("[ms] 项目文件夹同步失败（数据已写入，不回滚）：%s" % e)
    BUS.emit_latest(kind)


# ---------------------------------------------------------------- 页面
def _index_path() -> str:
    return os.path.join(WEB_DIR, INDEX_NAME)


_BUILD_RE = re.compile(r"var BUILD_ID\s*=\s*'([^']*)'")


def _build_id() -> str:
    try:
        with open(_index_path(), "r", encoding="utf-8") as f:
            m = _BUILD_RE.search(f.read())
        return m.group(1) if m else ""
    except Exception:
        return ""


async def home(request):
    """前端是**纯静态**的：一律返回 index.html，登录与否由前端自己问 /api/whoami。"""
    p = _index_path()
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return Response(f.read(), media_type="text/html; charset=utf-8")
    return Response(
        "<!DOCTYPE html><html><body style='font-family:sans-serif;padding:40px'>"
        "<h3>前端未部署</h3><p>缺少 <code>web/index.html</code>。</p>"
        "<p>API 可用：<a href='/api/health'>/api/health</a></p></body></html>",
        media_type="text/html; charset=utf-8", status_code=200)


# ---------------------------------------------------------------- 会话
async def login(request):
    if request.method == "GET":
        return JSONResponse({"ok": True, "hint": "POST user / pass（form 或 JSON）"})
    if not PASS:
        return JSONResponse({"ok": False, "error": "human channel disabled"}, status_code=403)
    try:
        if "application/json" in (request.headers.get("content-type") or ""):
            body = await request.json()
        else:
            body = dict(await request.form())
    except Exception:
        body = {}
    u = str(body.get("user") or "").strip()
    p = str(body.get("pass") or "")
    if u != USER or p != PASS:
        return JSONResponse({"ok": False, "error": "bad credentials"}, status_code=401)
    resp = JSONResponse({"ok": True, "who": u})
    resp.set_cookie(COOKIE, make_cookie(u), max_age=TTL, httponly=True, samesite="lax")
    return resp


async def logout(request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


async def whoami(request):
    me = authenticate(request)
    if not me:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse({"ok": True, **me,
                         "build": _build_id(),
                         "roles": list(_ROLE_RANK.keys())})


async def api_ping(request):
    """token 专用探活 —— 前端「验证」按钮用它做**端到端真验证**。"""
    row = ms_db.auth_token(extract_token(request))
    if not row:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse({"ok": True, "who": row["name"], "role": row["role"], "ts": ms_db.now()})


# ---------------------------------------------------------------- items（v1 兼容）
async def api_items(request):
    me, g = _need(request, "read")
    if g:
        return g
    q = request.query_params
    items = ms_db.list_items(status=q.get("status"), type=q.get("type"),
                             level=q.get("level"), project=q.get("project"),
                             q=q.get("q"), limit=-1)
    if q.get("note") == "0":
        items = [{k: v for k, v in it.items() if k != "备注"} for it in items]
    return JSONResponse({"count": len(items), "items": items})


async def api_build(request):
    return JSONResponse({"id": _build_id()})


async def api_update(request):
    me, g = _need(request, "agent")
    if g:
        return g
    try:
        body = await request.json()
        code = str(body.pop("code", "") or body.pop("编号", "") or "")
        fields = {k: v for k, v in body.items() if v not in (None, "")}
        r = ms_db.update_item(code, actor=me["who"], **fields)
        _after_write("update")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)


async def api_add(request):
    me, g = _need(request, "agent")
    if g:
        return g
    try:
        body = await request.json()
        r = ms_db.add_item(actor=me["who"], **body)
        _after_write("add")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)


async def api_close(request):
    me, g = _need(request, "agent")
    if g:
        return g
    try:
        body = await request.json()
        r = ms_db.close_item(str(body.get("code") or body.get("编号") or ""),
                             body.get("conclusion"), actor=me["who"])
        _after_write("close")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)


async def api_item(request):
    me, g = _need(request, "read")
    if g:
        return g
    it = ms_db.get_item(request.path_params["code"])
    if not it:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "item": it})


# ---------------------------------------------------------------- ★ 点名 / 拍板
def _wake(code: str, opinion: str, actor: str) -> list:
    """★ 点名后**主动唤醒 agent**：把任务 POST 到所有登记过的 hook。

    同步等（超时 5s）—— 老大要立刻在界面上看到「叫没叫醒」。
    没配 hook 时返回空列表，**零开销**。
    """
    it = ms_db.get_item(code) or {}
    payload = {
        "event": "mention",
        "code": code,
        "title": it.get("标题", ""),
        "opinion": opinion,
        "status": it.get("状态", ""),
        "project": it.get("项目", ""),
        "level": it.get("等级", ""),
        "actor": actor,
        "ts": ms_db.now(),
        "dashboard": "http://%s:%s/" % (PUBLIC_HOST, PORT),
        "howto": "带你的 token 调 GET /api/item/%s 拿备注全文；跑起来后把状态改成「进行中」，跑完 /api/close。" % code,
    }
    try:
        return ms_db.fire_agents(payload, timeout=5.0)
    except Exception as e:                                        # noqa: BLE001
        return [{"error": "%s: %s" % (type(e).__name__, e)}]


async def api_mention(request):
    """点名 = 「这条要做」 ⇒ 状态→**已点名** + 落 mention 事件 + **主动唤醒 agent**。"""
    me, g = _need(request, "agent")
    if g:
        return g
    try:
        body = await request.json()
        code = str(body.get("code") or body.get("编号") or "")
        opinion = body.get("opinion") or body.get("意见") or ""
        r = ms_db.mention_item(code, opinion, actor=me["who"])
        _after_write("mention")
        wake = _wake(code, opinion, me["who"])
        return JSONResponse({**r, "唤醒": wake})
    except MsError as e:
        return _err(e)


async def api_decide(request):
    """拍板 = 老大给决定 ⇒ 意见进备注 + 类型→待办 + decide 事件。"""
    me, g = _need(request, "agent")
    if g:
        return g
    try:
        body = await request.json()
        r = ms_db.decide_item(str(body.get("code") or body.get("编号") or ""),
                              body.get("decision") or body.get("意见") or body.get("拍板") or "",
                              actor=me["who"])
        _after_write("decide")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)


# ---------------------------------------------------------------- ★ 事件
async def api_events(request):
    me, g = _need(request, "read")
    if g:
        return g
    q = request.query_params
    try:
        since = int(q.get("since") or 0)
    except ValueError:
        since = 0
    unread = str(q.get("unread") or "") in ("1", "true", "yes")
    try:
        limit = int(q.get("limit") or 200)
    except ValueError:
        limit = 200
    r = ms_db.list_events(since=since, unread_only=unread, limit=limit)
    # ★ 用 token 拉活就留痕（这是"用 token 把握唤醒"的数据来源）；Cookie 登录不算 agent
    if me.get("via") == "token" and me.get("token_id"):
        try:
            ms_db.mark_pull(int(me["token_id"]))
        except Exception:                                         # noqa: BLE001
            pass
    return JSONResponse(r)


async def api_events_ack(request):
    me, g = _need(request, "agent")
    if g:
        return g
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        up_to = int(body.get("up_to") or body.get("upto") or 0)
    except (TypeError, ValueError):
        up_to = 0
    r = ms_db.ack_events(up_to=up_to, actor=me["who"])
    # ★ ack 才是"真消费到了" —— 记下游标，工作台就能算出这个 agent 落后几条
    if me.get("via") == "token" and me.get("token_id"):
        try:
            cur = up_to or ms_db.last_event().get("id", 0)
            ms_db.mark_pull(int(me["token_id"]), cur)
        except Exception:                                         # noqa: BLE001
            pass
    return JSONResponse(r)


async def api_stream(request):
    """SSE：老大一点名/拍板，订阅方秒级收到。"""
    me, g = _need(request, "read")
    if g:
        return g
    if EventSourceResponse is None:
        return JSONResponse({"ok": False, "error": "sse unavailable"}, status_code=501)

    q = asyncio.Queue(maxsize=256)

    async def gen():
        BUS.subs.add(q)
        try:
            yield {"event": "hello",
                   "data": json.dumps({"ok": True, "who": me["who"],
                                       "last_id": ms_db.last_event().get("id", 0)},
                                      ensure_ascii=False)}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield {"event": "changed", "data": json.dumps(ev, ensure_ascii=False)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            BUS.subs.discard(q)

    return EventSourceResponse(gen())


# ---------------------------------------------------------------- ★ tokens
async def api_tokens(request):
    if request.method == "GET":
        me, g = _need(request, "admin")
        if g:
            return g
        # 🔴 隐私防护（老大 2026-09-20）——列表**一律不带明文**，
        #    前端只拿到 tail 去拼 `ms_••••…xxxx`；明文走 /api/tokens/{id}/value 单取一次。
        return JSONResponse({"ok": True, "tokens": ms_db.list_tokens(reveal=False),
                             "note": "列表不含明文；复制请用 GET /api/tokens/{id}/value"})
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        body = await request.json()
        r = ms_db.add_token(body.get("name") or "", role=body.get("role") or "agent",
                            note=body.get("note") or "", actor=me["who"])
        _after_write("token")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)


async def api_token_rotate(request):
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        r = ms_db.rotate_token(int(request.path_params["tid"]), actor=me["who"])
        _after_write("token")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)


async def api_token_revoke(request):
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        r = ms_db.revoke_token(int(request.path_params["tid"]), actor=me["who"])
        _after_write("token")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)


async def api_token_value(request):
    """★ 单取 token 明文 —— 给「复制」按钮用（admin）。

    🔴 隐私防护：明文**只在这一个响应里出现**，前端拿到直接写剪贴板、**不渲染进 DOM**。
    每次调用都落一条审计事件（`action: reveal`），谁取的、什么时候，事件流里查得到。
    """
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        r = ms_db.get_token_value(int(request.path_params["tid"]),
                                  actor=me["who"], reason=request.query_params.get("why") or "")
        # 明文敏感：明确禁止任何中间层缓存
        return JSONResponse(r, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    except MsError as e:
        return _err(e)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)


async def api_token_verify_one(request):
    """★ 验证单个 token 是否可用 —— **不需要暴露明文**。

    内部走的就是 `/api/ping` 那条鉴权路径（`ms_db.auth_token`），所以等价于"真打一次"，
    但明文从头到尾不出库、不进响应、不进前端。
    """
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        tid = int(request.path_params["tid"])
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    # 直接查库拿明文 —— **只在后端内存里用**，绝不进响应
    with ms_db.conn() as c:
        r = c.execute("SELECT name, token, revoked FROM tokens WHERE id=?", (tid,)).fetchone()
    if not r:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if r["revoked"]:
        return JSONResponse({"ok": True, "id": tid, "name": r["name"],
                             "verify": False, "why": "已吊销"})
    got = ms_db.auth_token(r["token"], touch=False)
    return JSONResponse({"ok": True, "id": tid, "name": r["name"],
                         "verify": bool(got), "role": (got or {}).get("role", ""),
                         "why": "OK" if got else "鉴权未通过"})


async def api_token_verify(request):
    me, g = _need(request, "admin")
    if g:
        return g
    return JSONResponse(ms_db.verify_tokens())


# ---------------------------------------------------------------- ★ 改登录账号 / 密码
ENV_PATH = os.path.join(HERE, ".env")


def _write_env(key: str, value: str) -> dict:
    """改 `.env` 里一个键：**先备份、再原子替换**。键不存在则追加。

    ⚠️ 替换体必须用 `lambda` —— 直接用字符串的话，值里的 `\\` 会被 re 当转义吃掉。
    """
    import shutil
    if not os.path.exists(ENV_PATH):
        return {"ok": False, "error": "no .env", "path": ENV_PATH}
    bak = ENV_PATH + ".bak-" + time.strftime("%Y%m%d-%H%M%S", time.localtime())
    try:
        shutil.copyfile(ENV_PATH, bak)
        os.chmod(bak, 0o600)      # 🔴 备份里是明文密码，权限别比原件(600)松
    except OSError:
        bak = ""
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        s = f.read()
    new, n = re.subn(r"(?m)^%s=.*$" % re.escape(key), lambda m: "%s=%s" % (key, value), s)
    if n == 0:
        new = s.rstrip("\n") + "\n%s=%s\n" % (key, value)
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    os.replace(tmp, ENV_PATH)
    # 🔴 os.replace 换上来的 tmp 是**新建文件**（权限按 umask，通常是 644）⇒
    #    不显式 chmod 的话，每改一次密码就把 .env 的 600 放宽一次。踩过。
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    return {"ok": True, "backup": os.path.basename(bak) if bak else "", "replaced": n}


async def api_password(request):
    """★ 改登录账号 / 密码（老大要的「**支持自定义密码**」）。

    - **必须给旧密码** —— 否则任何碰到你已登录浏览器的人都能改掉它
    - 改完 **热生效**（不用重启），同时写回 `.env`（重启后仍是新值）
    - **`MS_SECRET` 不动** ⇒ 已登录的浏览器不必重新登录
    """
    me = authenticate(request)
    if not me:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:                                             # noqa: BLE001
        body = {}
    old = str(body.get("old") or "")
    new_u = str(body.get("user") or "").strip() or USER
    new_p = str(body.get("pass") or "")
    if old != PASS:
        return JSONResponse({"ok": False, "error": "旧密码不对"}, status_code=400)
    if not new_p.strip():
        return JSONResponse({"ok": False, "error": "新密码不能为空"}, status_code=400)

    did = {}
    if new_u != USER:
        did["user"] = _write_env("MS_USER", new_u)
    if new_p != PASS:
        did["pass"] = _write_env("MS_PASS", new_p)

    globals()["USER"] = new_u
    globals()["PASS"] = new_p                       # 热生效；SECRET 保持不变 ⇒ 登录态不掉
    return JSONResponse({"ok": True, "user": new_u, "changed": list(did), "env": did,
                         "note": "已热生效；.env 也写回（重启后仍是新值）；登录态不受影响"})


# ---------------------------------------------------------------- ★ 实例设置
async def api_settings(request):
    """★ 设置系统 —— 网页「设置」页签靠它（老大 2026-09-29：网页要有平时那种设置界面）。

    GET  → 当前值 + 默认值 + SCHEMA + 已改过的键。**前端照 SCHEMA 渲染**设置页，
           所以「有哪些键 / 能填什么」只有 ms_settings.py 一处权威。
    POST → 改设置（需要 **admin**）。整批校验、任一项非法整体拒绝；只落非默认值；
           一次写操作落一条事件（kind=setting），收尾走 _after_write（镜像 + SSE 广播）。
    """
    me, err = _need(request, "read")
    if err:
        return err

    if request.method == "GET":
        return JSONResponse({
            "ok": True,
            "settings": ms_db.settings_get_all(),
            "defaults": ms_settings.defaults(),
            "schema": ms_settings.schema_json(),
            "groups": ms_settings.GROUPS,
            "stored": ms_db.settings_rows(),
            "role": me["role"],
        })

    me, err = _need(request, "admin")
    if err:
        return err
    try:
        body = await request.json()
    except Exception:                                             # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    patch = body.get("settings") if isinstance(body.get("settings"), dict) else body
    patch = {k: v for k, v in (patch or {}).items() if k != "settings"}
    try:
        r = ms_db.settings_set(patch, actor=me["who"])
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e),
                             "keys": sorted(patch)}, status_code=400)
    if r["changed"]:
        _after_write("setting")
    return JSONResponse({"ok": True, **r, "defaults": ms_settings.defaults()})


# ---------------------------------------------------------------- ★ Agent 唤醒
async def api_agents(request):
    if request.method == "GET":
        me, g = _need(request, "read")
        if g:
            return g
        return JSONResponse({"ok": True,
                             "agents": ms_db.list_agents(reveal=(me["role"] == "admin"))})
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        body = await request.json()
        r = ms_db.add_agent(body.get("name") or "", hook_url=body.get("hook_url") or "",
                            token=body.get("token") or "", note=body.get("note") or "",
                            actor=me["who"])
        _after_write("agent")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)


async def api_agent_update(request):
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        body = await request.json()
        r = ms_db.update_agent(int(request.path_params["aid"]), actor=me["who"], **body)
        _after_write("agent")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)


async def api_agent_del(request):
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        r = ms_db.del_agent(int(request.path_params["aid"]), actor=me["who"])
        _after_write("agent")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)


async def api_agent_ping(request):
    """试打一次（UI 的「试一下」）—— 立即知道 hook 通不通。"""
    me, g = _need(request, "admin")
    if g:
        return g
    try:
        r = ms_db.ping_agent(int(request.path_params["aid"]))
        _after_write("agent")
        return JSONResponse(r)
    except MsError as e:
        return _err(e)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)


# ---------------------------------------------------------------- ★ 项目文件夹
def _proj_root() -> str:
    return os.environ.get("MS_PROJECTS") or PROJECTS_ROOT


async def api_projects(request):
    me, g = _need(request, "read")
    if g:
        return g
    root = _proj_root()
    out = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            files = []
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".md"):
                    continue
                fp = os.path.join(d, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                files.append({"name": fn, "size": st.st_size,
                              "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
            out.append({"project": name, "count": len(files),
                        "total": sum(f["size"] for f in files), "files": files})
    return JSONResponse({"ok": True, "root": root, "projects": out,
                         "total": sum(p["count"] for p in out)})


async def api_project_file(request):
    me, g = _need(request, "read")
    if g:
        return g
    rel = str(request.query_params.get("path") or "")
    root = os.path.realpath(_proj_root())
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        return JSONResponse({"ok": False, "error": "path escape"}, status_code=400)
    if not os.path.isfile(full) or not full.endswith(".md"):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    with open(full, "r", encoding="utf-8") as f:
        text = f.read()
    return JSONResponse({"ok": True, "path": rel, "size": len(text.encode("utf-8")),
                         "text": text})


async def api_stats(request):
    me, g = _need(request, "read")
    if g:
        return g
    st = ms_db.stats()
    st["build"] = _build_id()
    st["db"] = os.path.basename(ms_db.DB_PATH)
    return JSONResponse(st)


async def api_health(request):
    return JSONResponse({"ok": True, "server": "mainspring", "ver": ms_db.SCHEMA_VER,
                         "db": os.path.basename(ms_db.DB_PATH)})


async def api_guide(request):
    """★ 接入指南（`ms_guide.py` 是唯一真值源）。

    **故意免鉴权** —— 它的用途就是"还没接上时，先告诉你怎么接"。
    `?fmt=md` 出 Markdown（整篇或 `?topic=quickstart` 单节）；默认出 JSON。
    """
    q = request.query_params
    topic = q.get("topic") or ""
    if (q.get("fmt") or "").lower() in ("md", "markdown") or topic:
        from starlette.responses import PlainTextResponse
        return PlainTextResponse(ms_guide.render(topic),
                                 media_type="text/markdown; charset=utf-8")
    return JSONResponse(ms_guide.as_json())


# ---------------------------------------------------------------- 注册
def register_routes(app):
    add = app.add_route
    add("/", home, methods=["GET"])

    # 前端目录的静态资源（自检探针页 / 将来加 css·图片都走这里）
    if os.path.isdir(WEB_DIR):
        try:
            from starlette.staticfiles import StaticFiles
            app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
        except Exception:                      # noqa: BLE001
            pass

    add("/login", login, methods=["GET", "POST"])
    add("/logout", logout, methods=["GET"])

    add("/api/health", api_health, methods=["GET"])
    add("/api/guide", api_guide, methods=["GET"])
    add("/api/password", api_password, methods=["POST"])
    add("/api/settings", api_settings, methods=["GET", "POST"])
    add("/api/ping", api_ping, methods=["GET"])
    add("/api/whoami", whoami, methods=["GET"])
    add("/api/stats", api_stats, methods=["GET"])
    add("/api/build", api_build, methods=["GET"])

    add("/api/items", api_items, methods=["GET"])
    add("/api/item/{code}", api_item, methods=["GET"])
    add("/api/update", api_update, methods=["POST"])
    add("/api/add", api_add, methods=["POST"])
    add("/api/close", api_close, methods=["POST"])
    add("/api/mention", api_mention, methods=["POST"])
    add("/api/decide", api_decide, methods=["POST"])

    add("/api/events", api_events, methods=["GET"])
    add("/api/events/ack", api_events_ack, methods=["POST"])
    add("/api/stream", api_stream, methods=["GET"])

    add("/api/tokens", api_tokens, methods=["GET", "POST"])
    add("/api/tokens/verify", api_token_verify, methods=["GET"])
    add("/api/tokens/{tid:int}/value", api_token_value, methods=["GET"])
    add("/api/tokens/{tid:int}/verify", api_token_verify_one, methods=["GET", "POST"])
    add("/api/tokens/{tid:int}/rotate", api_token_rotate, methods=["POST"])
    add("/api/tokens/{tid:int}/revoke", api_token_revoke, methods=["POST"])

    add("/api/agents", api_agents, methods=["GET", "POST"])
    add("/api/agents/{aid:int}", api_agent_update, methods=["POST", "PATCH"])
    add("/api/agents/{aid:int}/del", api_agent_del, methods=["POST"])
    add("/api/agents/{aid:int}/ping", api_agent_ping, methods=["POST"])

    add("/api/projects", api_projects, methods=["GET"])
    add("/api/projects/file", api_project_file, methods=["GET"])
