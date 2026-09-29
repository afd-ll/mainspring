# -*- coding: utf-8 -*-
"""Mainspring 机枢 v2 · 服务器入口（三扇门 + 静态前端）

    ①  前端      `/`            —— **纯静态** `web/index.html`，登录与否由前端自己问 /api/whoami
    ②  人类 API  `/api/*` `/login` `/logout`  —— Cookie 或 Token（见 ms_api）
    ③  AI 通道   `/mcp`         —— MCP over HTTP，**多 token（查库鉴权）**
    ④  探活      `/health` `/api/health`      —— 免鉴权

与 v1 的关键差别：
    · 数据源 wb.json → **ms.sqlite3**（ms_db）
    · token 从 `.env::MS_TOKEN` 单值 → **tokens 表多值**，可在网页里增删改验证
    · 新增事件总线：写操作即时推给所有 SSE 订阅者（ms_api.BUS）

环境变量：MS_DB / MS_USER / MS_PASS / MS_SECRET / MS_PUBLIC_HOST / MS_HOST / MS_PORT
    直接 `python ms_server.py` 时会自动读仓库根的 .env（见 _load_dotenv）；
    用 systemd 部署则由 EnvironmentFile 注入，此时 .env 不会被文件覆盖。
    MS_TOKEN  可选：首次启动时把 v1 的旧 token 落库为一条 admin token（保证旧 agent 不断）
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load_dotenv(path: str) -> None:
    """极简 .env 加载器 —— 刻意不引 python-dotenv，这个项目保持零额外依赖。

    ⚠️ 必须在 import ms_api / ms_db / ms_mcp **之前**调用：那几个模块在导入时
    就已经 os.environ.get("MS_*") 把配置读走了，晚了就晚了。

    真实环境变量优先 —— systemd 的 EnvironmentFile 传进来的不会被文件覆盖。
    """
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv(os.path.join(HERE, ".env"))

import uvicorn  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

import ms_api  # noqa: E402
import ms_db  # noqa: E402
import ms_guide  # noqa: E402
import ms_mcp  # noqa: E402

HOST = os.environ.get("MS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MS_PORT", "8787"))
PUBLIC_HOST = os.environ.get("MS_PUBLIC_HOST", "127.0.0.1").strip()

# ---------- 建库（幂等）+ 迁移旧 token ----------
print("[ms] db = %s" % ms_db.init_db()["db"], flush=True)
_old = os.environ.get("MS_TOKEN", "").strip()
if _old:
    r = ms_db.ensure_admin_token(_old)
    print("[ms] 旧 MS_TOKEN 落库：%s" % ("已存在 id=%s" % r.get("id") if r.get("existed")
                                        else "新增 id=%s" % r.get("id")), flush=True)
print("[ms] tokens = %d（未吊销）" % ms_db.stats()["tokens"], flush=True)

# ---------- MCP（放行外网 Host，否则 421 Invalid Host header）----------
ms_mcp.mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", PUBLIC_HOST, PUBLIC_HOST + ":*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*",
                     "http://" + PUBLIC_HOST, "http://" + PUBLIC_HOST + ":*"],
)

app = ms_mcp.mcp.streamable_http_app()
ms_api.register_routes(app)

NOISE = ("/favicon.ico", "/robots.txt", "/apple-touch-icon.png",
         "/apple-touch-icon-precomposed.png")
PASSTHRU = ("/", "/login", "/logout")


class Gate(BaseHTTPMiddleware):
    """只拦 AI 通道（/mcp 等）；前端与 /api/* 交给 ms_api 自己按 Cookie/Token 鉴权。"""

    async def dispatch(self, request, call_next):
        # 事件总线绑定当前事件循环（中间件一定跑在 async 上下文里）
        ms_api.BUS.bind(asyncio.get_running_loop())

        path = request.url.path
        if path in ("/health", "/api/health"):
            return JSONResponse({"ok": True, "server": "mainspring", "ver": ms_db.SCHEMA_VER})
        if path in NOISE:
            return Response(status_code=204)
        if path in PASSTHRU or path.startswith("/api/"):
            return await call_next(request)

        row = ms_db.auth_token(ms_api.extract_token(request))
        if not row:
            return JSONResponse(
                {"ok": False, "error": "unauthorized",
                 "hint": "AI 通道需 X-MS-Token / Authorization: Bearer / ?token=；"
                         "浏览器请直接打开 /"},
                status_code=401)
        if row["role"] == "readonly":
            return JSONResponse({"ok": False, "error": "forbidden",
                                 "detail": "readonly token 不能走 MCP 通道"}, status_code=403)
        return await call_next(request)


app.add_middleware(Gate)


if __name__ == "__main__":
    # 与「接入指南」共用同一个基址来源，否则设了 MS_PUBLIC_URL 之后日志和指南会各说一个地址
    BASE = ms_guide.base_url()
    print("[ms] web    = %s/            (静态前端)" % BASE, flush=True)
    print("[ms] human  = %s/api/*       (Cookie 或 Token)" % BASE, flush=True)
    print("[ms] ai     = %s/mcp         (多 token 查库鉴权)" % BASE, flush=True)
    print("[ms] allowed_hosts = %s" % ms_mcp.mcp.settings.transport_security.allowed_hosts, flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
