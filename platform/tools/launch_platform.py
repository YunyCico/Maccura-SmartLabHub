"""SmartLabHub 工作平台启动器。

职责：启动后端 FastAPI、Vue/Vite 前端，轮询健康检查，通过后打开浏览器。
批处理只负责调用本文件，避免在 GBK 代码页下解析中文和复杂引号。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # 项目根（platform/tools -> platform -> 项目根）
LOG_DIR = ROOT / "logs"

# ★ 运行时不再写死绝对路径。
#   早期版本写死成开发机的 WorkBuddy 托管路径，整个目录打包给同事后
#   必然报 "Python runtime not found" —— 同事装了 Python 也没用，因为压根没去找。
#   现在按 runtime_probe 的优先级探测（项目 venv > SLH_PYTHON > py -3 > PATH > 常见目录）。
from runtime_probe import detect_node, detect_python  # noqa: E402

PYTHON_EXE = detect_python(ROOT) or Path("python")
NODE_EXE = detect_node(ROOT) or Path("node")
VITE_BIN = ROOT / "home" / "node_modules" / "vite" / "bin" / "vite.js"
# 前端构建产物：发布模式下由后端直接托管，同事就不需要装 Node
DIST_DIR = ROOT / "home" / "dist"
BACKEND_DIR = ROOT / "platform" / "backend"
FRONTEND_DIR = ROOT / "home"

BACKEND_PORT = 8000
FRONTEND_PORT = 5173
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"
BACKEND_HEALTH = f"{BACKEND_URL}/api/health"

# 用代理绕过变量保证本地请求不被本机代理接管。
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, timeout: float = 2.0) -> tuple[int, str] | None:
    try:
        with OPENER.open(url, timeout=timeout) as response:
            body = response.read(200_000)
            return response.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(20_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return error.code, body
    except Exception:
        return None


def check(url: str, needle: str) -> bool:
    result = fetch(url)
    if not result:
        return False
    status, body = result
    return status == 200 and needle in body


def backend_ready() -> bool:
    return check(BACKEND_HEALTH, '"status"')


def frontend_ready() -> bool:
    return check(f"{FRONTEND_URL}/", 'id="app"')


def embed_ready() -> bool:
    result = fetch(f"{BACKEND_URL}/api/data-analysis/embed-status")
    if not result:
        return False
    status, body = result
    if status != 200 or '"running"' not in body:
        return False
    try:
        import json

        payload = json.loads(body)
    except Exception:
        return False
    url = str(payload.get("url") or "")
    if not url:
        return False
    page = fetch(url)
    if not page:
        return False
    page_status, page_body = page
    if page_status != 200:
        return False
    # 原生首页标题为中文，且 index.html 可能不在首屏字节内。
    if "SmartLabHub" in page_body or "智慧实验室" in page_body:
        return True
    if "app.js" in page_body:
        try:
            script = fetch(url.rstrip("/") + "/app.js")
        except Exception:
            return False
        if script and script[0] == 200:
            return True
    return False


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# 后端运行必需的第三方包。缺任何一个都起不来。
_REQUIRED_PACKAGES = ("fastapi", "uvicorn", "pandas", "openpyxl", "httpx")
VENV_DIR = BACKEND_DIR / ".venv"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"


def _missing_packages(python_exe: Path) -> list[str]:
    """列出当前解释器缺哪些必需包（不真正 import，只查元数据，快）。"""
    code = (
        "import importlib.util,json,sys;"
        "print(json.dumps([m for m in %r if importlib.util.find_spec(m) is None]))"
        % (list(_REQUIRED_PACKAGES),)
    )
    try:
        out = subprocess.run(
            [str(python_exe), "-c", code], capture_output=True, text=True, timeout=60
        )
        return json.loads((out.stdout or "[]").strip() or "[]")
    except Exception:
        # 查不出来就当作不缺，交给后面的启动报错去暴露问题
        return []


def bootstrap_dependencies() -> Path | None:
    """确保依赖可用；缺了就建本地 venv 装好，然后返回该 venv 的解释器。

    为什么不直接在 .bat 里做：
    .bat 必须保持纯 ASCII 且极薄（GBK 代码页下非 ASCII 会乱码），
    复杂逻辑一律放 Python 侧。

    为什么不装到系统 Python：
    同事的机器上直接往系统 Python 装包可能没权限，
    而且会污染他们的其他项目。装到项目内的 .venv 最干净。
    """
    missing = _missing_packages(PYTHON_EXE)
    if not missing:
        return None
    log(f"[SETUP] missing dependencies: {', '.join(missing)}")
    log("")
    log("  首次运行需要下载依赖，可能要几分钟，请耐心等待。")
    log("  （看到 Downloading / Installing / Successfully 就是正常的，在动。）")
    log("  如果画面不动：按一下回车，或用鼠标点一下控制台窗口。")
    log("  窗口标题出现「选择」两个字 = 控制台被鼠标点进了选择模式，")
    log("  按一下 Esc 键退出选择模式即可恢复。")
    log("")

    if not VENV_PY.is_file():
        log("[SETUP] creating a local environment ...")
        try:
            subprocess.run(
                [str(PYTHON_EXE), "-m", "venv", str(VENV_DIR)],
                check=True, timeout=300,
            )
        except Exception as error:
            log(f"[ERROR] could not create the local environment: {error}")
            log(f"        install these manually: pip install {' '.join(missing)}")
            return None

    try:
        # ★ 不捕获输出：pip 的下载进度要直接显示给用户。
        #   之前用 capture_output=True 把输出吞了，屏幕上几分钟没动静，
        #   同事会以为程序卡死（实际上只是安静地下着包）。
        log("[SETUP] upgrading pip ...")
        subprocess.run(
            [str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"],
            check=False, timeout=600,
        )

        # 默认走国内镜像（官方 PyPI 在国内很慢，慢到让人以为卡死），
        # 镜像失败再回退官方源。SLH_PIP_INDEX_URL 可以覆盖。
        index_url = os.environ.get("SLH_PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
        requirements = str(BACKEND_DIR / "requirements.txt")
        log(f"[SETUP] installing dependencies (this is the slow part) ...")
        log(f"        source: {index_url}")
        result = subprocess.run(
            [str(VENV_PY), "-m", "pip", "install", "-r", requirements,
             "-i", index_url, "--trusted-host", "pypi.tuna.tsinghua.edu.cn"],
            check=False, timeout=1800,
        )
        if result.returncode != 0 and index_url != "https://pypi.org/simple":
            log("[SETUP] mirror failed, retrying with the official index ...")
            result = subprocess.run(
                [str(VENV_PY), "-m", "pip", "install", "-r", requirements],
                check=False, timeout=1800,
            )
        if result.returncode != 0:
            log(f"[ERROR] dependency installation failed (pip exit {result.returncode})")
            log(f"        install these manually: pip install {' '.join(missing)}")
            return None
    except Exception as error:
        log(f"[ERROR] dependency installation failed: {error}")
        return None

    log("[SETUP] dependencies installed.")
    return VENV_PY


def preflight(release: bool = False) -> list[str]:
    problems = []
    if not PYTHON_EXE.is_file():
        problems.append(f"Python runtime missing: {PYTHON_EXE}")
    if release:
        # 发布模式：后端直接托管 home/dist，同事不需要 Node
        if not (DIST_DIR / "index.html").is_file():
            problems.append(f"Frontend build missing: {DIST_DIR / 'index.html'}")
    else:
        if not NODE_EXE.is_file():
            problems.append(f"Node runtime missing: {NODE_EXE}")
        if not VITE_BIN.is_file():
            problems.append(f"Vite entry missing: {VITE_BIN} (run npm install in home/)")
    if not (BACKEND_DIR / "app" / "main.py").is_file():
        problems.append(f"Backend entry missing: {BACKEND_DIR / 'app' / 'main.py'}")
    return problems


def tail(path: Path, limit: int = 25) -> str:
    if not path.is_file():
        return f"(no log file: {path})"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as error:  # pragma: no cover - 读取日志失败时给出提示
        return f"(cannot read log: {error})"
    if not lines:
        return "(log is empty)"
    return "\n".join(lines[-limit:])


def spawn(label: str, cwd: Path, args: list[str], log_file: Path) -> subprocess.Popen:
    handle = log_file.open("w", encoding="utf-8", errors="replace")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    log(f"  starting {label} ...")
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def wait_until(ready, label: str, timeout: float, processes: list[subprocess.Popen]) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready():
            log(f"  [OK] {label}")
            return True
        for proc in processes:
            if proc.poll() is not None:
                log(f"  [FAIL] {label} process exited with code {proc.returncode}")
                return False
        time.sleep(0.5)
    log(f"  [TIMEOUT] {label} not ready in {timeout:.0f}s")
    return False


def open_browser_at(url: str) -> None:
    """打开指定地址。发布模式用后端端口，开发模式用 vite 端口。"""
    try:
        import webbrowser

        webbrowser.open(url)
        log(f"  browser opened: {url}")
    except Exception as error:  # pragma: no cover - 打开浏览器失败不阻断服务
        log(f"  [WARN] could not open browser automatically: {error}")
        log(f"  please open manually: {url}")


def open_browser() -> None:
    try:
        import webbrowser

        webbrowser.open(f"{FRONTEND_URL}/")
        log(f"  browser opened: {FRONTEND_URL}/")
    except Exception as error:  # pragma: no cover - 打开浏览器失败不阻断服务
        log(f"  [WARN] could not open browser automatically: {error}")
        log(f"  please open manually: {FRONTEND_URL}/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the SmartLabHub platform")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait per service")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--reuse", action="store_true", help="reuse already running services")
    parser.add_argument(
        "--release",
        action="store_true",
        help="release mode: serve the built frontend from the backend, no Node needed",
    )
    args = parser.parse_args()

    release = bool(args.release)

    # 依赖自举：同事只装 Python 就能跑，缺的包装到项目内 .venv
    venv_python = bootstrap_dependencies()
    if venv_python is not None:
        global PYTHON_EXE
        PYTHON_EXE = venv_python

    log("=" * 62)
    log("SmartLabHub Work Platform Launcher")
    log(f"root: {ROOT}")
    log(f"mode: {'release (backend serves home/dist)' if release else 'development (vite dev server)'}")
    log(f"python: {PYTHON_EXE}")
    if not release:
        log(f"node:   {NODE_EXE}")
    log("=" * 62)

    problems = preflight(release)
    if problems:
        for item in problems:
            log(f"[ERROR] {item}")
        log("Startup aborted.")
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    backend_log = LOG_DIR / "backend.log"
    frontend_log = LOG_DIR / "frontend.log"

    # 发布模式：前端由后端直接托管（home/dist），只起后端一个进程
    if release:
        if backend_ready():
            log(f"  [OK] backend already serving {BACKEND_HEALTH}")
        else:
            if port_busy(BACKEND_PORT):
                log(f"[ERROR] port {BACKEND_PORT} is occupied by an unknown process.")
                log("        close it (or restart Windows) and run this file again.")
                return 3
            processes = [
                spawn(
                    "backend FastAPI (serving home/dist)",
                    BACKEND_DIR,
                    [
                        str(PYTHON_EXE),
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--app-dir",
                        str(BACKEND_DIR),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(BACKEND_PORT),
                    ],
                    backend_log,
                )
            ]
            log("  waiting for backend ...")
            if not wait_until(backend_ready, f"backend {BACKEND_HEALTH}", args.timeout, processes):
                log("[ERROR] backend failed to start. Last log lines:")
                log(tail(backend_log))
                return 5
        log("")
        log(f"  平台已就绪： {BACKEND_URL}/")
        if not args.no_browser:
            open_browser_at(BACKEND_URL)
        return 0

    if backend_ready() and frontend_ready():
        log("[INFO] services already running, reusing them")
        if not args.no_browser:
            open_browser()
        return 0

    if args.reuse and (backend_ready() or frontend_ready()):
        log("[INFO] partial services detected, reusing and starting the rest")

    processes: list[subprocess.Popen] = []

    spawned_backend = False
    if not backend_ready():
        if port_busy(BACKEND_PORT):
            log(f"[ERROR] port {BACKEND_PORT} is occupied by an unknown process.")
            log("        close it (or restart Windows) and run this file again.")
            return 3
        processes.append(
            spawn(
                "backend FastAPI",
                BACKEND_DIR,
                [
                    str(PYTHON_EXE),
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--app-dir",
                    str(BACKEND_DIR),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(BACKEND_PORT),
                ],
                backend_log,
            )
        )
        spawned_backend = True
    else:
        log(f"  [OK] backend already serving {BACKEND_HEALTH}")

    # ★ 后端没 ready 之前**不要**起前端。
    #   前端 Vite 一起来，浏览器里已经打开的平台页会立刻发请求，
    #   这时后端还没起来 → 首页弹「后端服务暂时不可用」，
    #   要等下一次轮询才恢复（此前还会一直挂着不消失）。
    if spawned_backend:
        log("  waiting for backend before starting frontend ...")
        if not (
            backend_ready()
            or wait_until(
                backend_ready, f"backend {BACKEND_HEALTH}", args.timeout, processes
            )
        ):
            log("[ERROR] backend failed to start. Last log lines:")
            log(tail(backend_log))
            return 5

    if not frontend_ready():
        if port_busy(FRONTEND_PORT):
            log(f"[ERROR] port {FRONTEND_PORT} is occupied by an unknown process.")
            log("        close it (or restart Windows) and run this file again.")
            return 4
        processes.append(
            spawn(
                "frontend Vite",
                FRONTEND_DIR,
                [
                    str(NODE_EXE),
                    str(VITE_BIN),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(FRONTEND_PORT),
                    "--strictPort",
                ],
                frontend_log,
            )
        )
    else:
        log(f"  [OK] frontend already serving {FRONTEND_URL}")

    if processes:
        log("waiting for services ...")

    backend_ok = backend_ready() or wait_until(
        backend_ready, f"backend {BACKEND_HEALTH}", args.timeout, processes
    )
    if not backend_ok:
        log("[ERROR] backend failed to start. Last log lines:")
        log(tail(backend_log))
        return 5

    frontend_ok = frontend_ready() or wait_until(
        frontend_ready, f"frontend {FRONTEND_URL}", args.timeout, processes
    )
    if not frontend_ok:
        log("[ERROR] frontend failed to start. Last log lines:")
        log(tail(frontend_log))
        return 6

    embed_ok = embed_ready() or wait_until(
        embed_ready, "SmartLabHub embedded page", args.timeout, processes
    )
    if not embed_ok:
        log("[WARN] embedded SmartLabHub page not confirmed yet.")
        log("       the platform page can still trigger it on demand.")
        log("backend log tail:")
        log(tail(backend_log))

    log("-" * 62)
    log(f"[OK] backend : {BACKEND_HEALTH}")
    log(f"[OK] frontend: {FRONTEND_URL}/")
    if embed_ok:
        status = fetch(f"{BACKEND_URL}/api/data-analysis/embed-status")
        if status:
            import json

            try:
                log(f"[OK] embedded: {json.loads(status[1]).get('url')}")
            except Exception:
                log("[OK] embedded: SmartLabHub page")
    log(f"[OK] logs    : {LOG_DIR}")
    log("-" * 62)

    if not args.no_browser:
        open_browser()

    log("All services are running. Keep this window open is NOT required.")
    log("To stop everything, close this file or run 停止平台.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())