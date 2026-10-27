from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.responses import JSONResponse

from app.api import auth, dashboard, data_analysis, health, import_preview, imports, integrations, messages, user
from app.core.config import get_settings
from app.core.database import initialize_database
from app.services import auth_service
from app.services.dingtalk_stream import start_dingtalk_stream
from app.services.smartlab_service import initialize_engine, start_embed_server

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")
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
    if request.method == "OPTIONS" or not path.startswith("/api/"):
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
        candidate = (_DIST_DIR / full_path).resolve()
        try:
            # 防目录穿越：解析后的路径必须仍在 dist 目录内
            candidate.relative_to(_DIST_DIR.resolve())
        except ValueError:
            candidate = _DIST_DIR / "index.html"
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST_DIR / "index.html")
