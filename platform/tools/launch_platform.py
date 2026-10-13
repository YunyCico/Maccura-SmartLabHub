"""SmartLabHub 工作平台启动器。

职责：启动后端 FastAPI、Vue/Vite 前端，轮询健康检查，通过后打开浏览器。
批处理只负责调用本文件，避免在 GBK 代码页下解析中文和复杂引号。
"""
from __future__ import annotations

import argparse
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

PYTHON_EXE = Path(
    r"C:\Users\98319\.workbuddy\binaries\python\envs\smartlabhub\Scripts\python.exe"
)
NODE_EXE = Path(
    r"C:\Users\98319\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
)
VITE_BIN = ROOT / "home" / "node_modules" / "vite" / "bin" / "vite.js"
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


def preflight() -> list[str]:
    problems = []
    if not PYTHON_EXE.is_file():
        problems.append(f"Python runtime missing: {PYTHON_EXE}")
    if not NODE_EXE.is_file():
        problems.append(f"Node runtime missing: {NODE_EXE}")
    if not VITE_BIN.is_file():
        problems.append(f"Vite entry missing: {VITE_BIN}")
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
    args = parser.parse_args()

    log("=" * 62)
    log("SmartLabHub Work Platform Launcher")
    log(f"root: {ROOT}")
    log("=" * 62)

    problems = preflight()
    if problems:
        for item in problems:
            log(f"[ERROR] {item}")
        log("Startup aborted.")
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    backend_log = LOG_DIR / "backend.log"
    frontend_log = LOG_DIR / "frontend.log"

    if backend_ready() and frontend_ready():
        log("[INFO] services already running, reusing them")
        if not args.no_browser:
            open_browser()
        return 0

    if args.reuse and (backend_ready() or frontend_ready()):
        log("[INFO] partial services detected, reusing and starting the rest")

    processes: list[subprocess.Popen] = []

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
    else:
        log(f"  [OK] backend already serving {BACKEND_HEALTH}")

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