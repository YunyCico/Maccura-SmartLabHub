from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.responses import JSONResponse, Response

from app.api import auth, dashboard, data_analysis, health, import_preview, imports, integrations, messages, user
from app.core.config import get_settings
from app.core.database import initialize_database
from app.services import auth_service
from app.services.dingtalk_stream import start_dingtalk_stream
from app.services.smartlab_service import initialize_engine, start_embed_server

settings = get_settings()

# 生产环境默认把 /docs、/openapi.json 关掉：那等于把整套接口说明书
# （含参数名、字段名）公开给任何能访问到的人。调试时用 EXPOSE_API_DOCS=true 打开。
_docs_kwargs = (
    {"docs_url": "/docs", "openapi_url": "/openapi.json", "redoc_url": "/redoc"}
    if settings.expose_api_docs
    else {"docs_url": None, "openapi_url": None, "redoc_url": None}
)
app = FastAPI(title=settings.app_name, version="0.1.0", **_docs_kwargs)
initialize_database()
initialize_engine()
start_embed_server()
# 钉钉事件推送采集（自动记录每条日程的创建人）。失败不影响主应用。
start_dingtalk_stream()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# 登录守卫
#
# 放在中间件而不是逐个路由加依赖：一个地方就能看清「哪些不用登录」，
# 以后新加接口默认就是被保护的（不会漏）。
#
# 公开白名单只有四类：
#   1. /api/auth/     登录页自己要用的接口
#   2. /api/health    启动器靠它判断后端就绪
#   3. /api/data-analysis/embed-status  启动器靠它判断嵌入式工作台
#   4. /api/dingtalk/config            登录前要知道是否走钉钉免登
# 非 /api/ 路径是前端静态资源（登录页本身），不拦。
# ------------------------------------------------------------------
_PUBLIC_API_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/api/data-analysis/embed-status",
    "/api/dingtalk/config",
)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # ⚠️ 除了 /api/，还要管 /engine/ 和 /portal/：
    #    /engine/ 是数据分析引擎的反向代理入口，
    #    /portal/ 是「服务需求报表」子应用，
    #    它们都挂着**没有鉴权**的内部接口，漏掉等于白加鉴权。
    guarded = (
        path.startswith("/api/")
        or path.startswith("/engine/")
        or path == "/portal"
        or path.startswith("/portal/")
    )
    if request.method == "OPTIONS" or not guarded:
        return await call_next(request)

    # ★ 先无条件解析会话：即使这个路径是公开的（比如 /api/auth/me），
    #   接口内部也可能需要知道"当前是谁"，所以状态要挂上。
    #   空 token 由 resolve_session 自己返回 None，不要在外面用 if 短路
    #   （短路会让测试里的会话旁路失效）。
    token = request.cookies.get(auth_service.SESSION_COOKIE, "")
    user = auth_service.resolve_session(token)
    request.state.user = user

    if any(path.startswith(prefix) for prefix in _PUBLIC_API_PREFIXES):
        return await call_next(request)

    if not user:
        return JSONResponse(
            {"detail": "未登录或登录已过期，请重新登录", "need_login": True},
            status_code=401,
        )
    return await call_next(request)


app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(user.router, prefix="/api")
app.include_router(import_preview.router, prefix="/api")
app.include_router(imports.router, prefix="/api")
app.include_router(data_analysis.router, prefix="/api")
app.include_router(messages.router, prefix="/api")
app.include_router(integrations.router, prefix="/api")


# ------------------------------------------------------------------
# 数据分析引擎反向代理（/engine/* -> 127.0.0.1:8765）
#
# 为什么需要：引擎自带一个 HTTP 服务，原来只绑 127.0.0.1，前端 iframe 直接用
# `http://127.0.0.1:8765/`。这在"只在自己电脑上跑"时没问题，但一旦部署到服务器、
# 同事从自己电脑访问，那个 127.0.0.1 就变成了**他自己电脑**，iframe 必然白屏。
#
# 现在统一从 /engine/ 走：浏览器只访问平台这一个源，引擎的端口不出本机。
# 附带好处是登录中间件同样管住它 —— 引擎的接口以前是完全裸奔的。
#
# 必须在 SPA fallback 之前注册，否则会被 /{full_path:path} 截走。
# ------------------------------------------------------------------
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


@app.api_route(
    "/engine/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def engine_proxy(rest: str, request: Request):
    """把 /engine/xxx 转发给本机的数据分析引擎，路径原样传递。"""
    import httpx

    from app.services import smartlab_service

    # ★ 用引擎的实际端口，不是配置里的 8765：
    #   8765 被占用时引擎会自己换一个，写死就会代理到错的地方。
    port = smartlab_service.engine_port()
    if not port:
        return JSONResponse(
            {"detail": "数据分析引擎没有启动，请查看 backend 日志。"}, status_code=503
        )

    url = f"http://127.0.0.1:{port}/{rest}"
    if request.url.query:
        url += "?" + request.url.query

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            upstream = await client.request(
                request.method, url, headers=headers, content=body, follow_redirects=False
            )
    except httpx.HTTPError as exc:
        return JSONResponse(
            {
                "detail": "数据分析引擎没有响应，请确认 backend 已正常启动。",
                "error": str(exc),
            },
            status_code=502,
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ------------------------------------------------------------------
# 「服务需求报表」子应用（/portal）
#
# 来源：原 DataFetchPortal（数据中台 · 对外数据拉取控制台）。
# 它本身是一个独立的 FastAPI + Vue 应用，这里直接**挂载进同一个进程**，
# 不另开端口 —— 好处是登录守卫自动覆盖它的接口，也不用多管一个进程。
#
# 挂载前做了两件事（改在 modules/service_demand 里）：
#   1. 包名 app → service_portal：和平台后端的 app 包重名会 import 错
#   2. 前端 vite base 改成 './'，api.js 按 /portal 前缀拼接口地址
#
# ⚠️ 必须注册在 SPA fallback 之前，否则 /portal/... 会被 /{full_path:path} 截走。
# ------------------------------------------------------------------
_SERVICE_DEMAND_BACKEND = PROJECT_ROOT / "modules" / "service_demand" / "backend"
if (_SERVICE_DEMAND_BACKEND / "service_portal" / "main.py").is_file():
    _sd_path = str(_SERVICE_DEMAND_BACKEND)
    if _sd_path not in sys.path:
        sys.path.insert(0, _sd_path)
    try:
        from service_portal.main import app as service_demand_app

        app.mount("/portal", service_demand_app, name="service-demand")
    except Exception as error:  # noqa: BLE001 - 子应用坏了不该拖垮主平台
        # 只记日志：平台其他功能照常可用，用户点进该模块会看到 404
        print(f"[WARN] 服务需求报表模块未能挂载：{error}")


# ------------------------------------------------------------------
# 前端静态资源托管（发布模式）
#
# 目的：把整个目录打包给同事时，同事只装 Python 就能跑，不需要 Node。
# 开发模式仍走 Vite dev server（5173），这里只在 home/dist 存在时挂载，
# 且只处理非 /api 路径，不影响后端接口。
#
# 前端是 history 路由（/settings/integrations 等），
# 所以未知路径要 fallback 到 index.html，否则刷新子页面会 404。
# ------------------------------------------------------------------
_DIST_DIR = PROJECT_ROOT / "home" / "dist"
if (_DIST_DIR / "index.html").is_file():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/assets",
        StaticFiles(directory=str(_DIST_DIR / "assets")),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """把非接口路径交给前端路由处理。"""
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # /engine 和 /portal 是内嵌子应用，必须由各自的挂载点处理。
        # 万一挂载没成功（比如模块目录缺失），绝不能把平台自己的 index.html
        # 当成它们的页面返回 —— 那会让 iframe 里出现"平台套平台"，
        # 看着像渲染 bug，实际是路由回落。宁可明确 404。
        if full_path.startswith(("engine/", "portal/")) or full_path in ("engine", "portal"):
            return JSONResponse(
                {"detail": "内嵌模块未挂载，请检查后端启动日志", "path": full_path},
                status_code=404,
            )
        candidate = (_DIST_DIR / full_path).resolve()
        try:
            # 防目录穿越：解析后的路径必须仍在 dist 目录内
            candidate.relative_to(_DIST_DIR.resolve())
        except ValueError:
            candidate = _DIST_DIR / "index.html"
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST_DIR / "index.html")
