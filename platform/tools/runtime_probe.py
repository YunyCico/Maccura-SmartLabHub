"""运行时探测：找到可用的 Python / Node。

为什么需要这个文件：
`启动平台.bat` 早期把 Python 和 Node 写死成了**开发机的绝对路径**，
把整个目录打包给同事后必然报"未安装 Python"——
同事其实装了，只是脚本压根没去找。

这里把探测逻辑集中成一处，批处理与 Python 启动器共用同一套优先级：

Python：
1. 项目本地 venv `platform/backend/.venv/Scripts/python.exe`（推荐，不会污染系统环境）
2. 环境变量 `SLH_PYTHON` / `PYTHON`
3. Windows Python Launcher `py -3`
4. PATH 上的 `python`
5. 常见安装目录 `%LOCALAPPDATA%\\Programs\\Python\\Python3*` 等

Node（只在开发模式需要，发布模式由后端直接托管前端产物）：
1. 环境变量 `SLH_NODE` / `NODE`
2. PATH 上的 `node`
3. 常见安装目录 `C:\\Program Files\\nodejs\\node.exe` 等
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows Store 的 python.exe / node.exe 是"占位符"，
# 双击会跳转应用商店而不是真的执行解释器，必须排除。
_STORE_STUB_MARKERS = ("\\WindowsApps\\", "\\Microsoft\\WindowsApps\\")


def _is_store_stub(path: str | os.PathLike[str]) -> bool:
    text = str(path)
    return any(marker in text for marker in _STORE_STUB_MARKERS)


def _version_of(executable: str) -> tuple[int, ...]:
    """取 Python 版本号；失败返回空元组。"""
    try:
        out = subprocess.run(
            [executable, "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return ()
    text = (out.stdout or "").strip()
    if not text:
        return ()
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()


def _windows_candidates() -> list[Path]:
    """常见 Python 安装目录。"""
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if local:
        roots.append(Path(local) / "Programs" / "Python")
    if program_files:
        roots.append(Path(program_files) / "Python")
    if program_files_x86:
        roots.append(Path(program_files_x86) / "Python")
    roots.append(Path("C:/Python313"))
    roots.append(Path("C:/Python312"))
    roots.append(Path("C:/Python311"))

    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            # 版本号大的排前面（3.13 > 3.12）
            children = sorted(
                (child for child in root.iterdir() if child.is_dir()),
                key=lambda p: _version_of(p / "python.exe") or (0,),
                reverse=True,
            )
        except OSError:
            continue
        for child in children:
            exe = child / "python.exe"
            if exe.is_file():
                found.append(exe)
    return found


def detect_python(project_root: Path | None = None) -> Path | None:
    """按优先级找一个能用的 Python 解释器。"""
    root = project_root or Path(__file__).resolve().parents[2]

    # 1) 项目本地 venv
    venv = root / "platform" / "backend" / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return venv

    # 2) 环境变量
    for key in ("SLH_PYTHON", "PYTHON"):
        value = os.environ.get(key)
        if value:
            candidate = Path(value)
            if candidate.is_file():
                return candidate

    # 3) Windows Python Launcher
    launcher = shutil.which("py")
    if launcher:
        try:
            out = subprocess.run(
                [launcher, "-3", "-c", "import sys;print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            text = (out.stdout or "").strip()
            if text:
                candidate = Path(text)
                if candidate.is_file() and not _is_store_stub(candidate):
                    return candidate
        except Exception:
            pass

    # 4) PATH 上的 python
    on_path = shutil.which("python")
    if on_path and not _is_store_stub(on_path):
        return Path(on_path)
    on_path3 = shutil.which("python3")
    if on_path3 and not _is_store_stub(on_path3):
        return Path(on_path3)

    # 5) 常见安装目录
    for candidate in _windows_candidates():
        if _is_store_stub(candidate):
            continue
        return candidate

    return None


def detect_node(project_root: Path | None = None) -> Path | None:
    """找一个能用的 Node。发布模式（后端直接托管前端）不需要它。"""
    for key in ("SLH_NODE", "NODE"):
        value = os.environ.get(key)
        if value:
            candidate = Path(value)
            if candidate.is_file():
                return candidate

    on_path = shutil.which("node")
    if on_path and not _is_store_stub(on_path):
        return Path(on_path)

    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    local = os.environ.get("LOCALAPPDATA")
    if program_files:
        candidates.append(Path(program_files) / "nodejs" / "node.exe")
    if program_files_x86:
        candidates.append(Path(program_files_x86) / "nodejs" / "node.exe")
    if local:
        candidates.append(Path(local) / "Programs" / "nodejs" / "node.exe")
    candidates.append(Path("C:/nodejs/node.exe"))
    for candidate in candidates:
        if candidate.is_file() and not _is_store_stub(candidate):
            return candidate
    return None


def missing_python_hint() -> str:
    """给用户看的中文提示：要装什么、怎么装、装完要做什么。"""
    return (
        "没有找到可用的 Python 解释器。\n"
        "  1) 打开 https://www.python.org/downloads/ 下载 Python 3.11 或更高版本；\n"
        "  2) 安装时务必勾选「Add python.exe to PATH」；\n"
        "  3) 安装完成后重新双击「启动平台.bat」即可。\n"
        "  也可以手动指定：set SLH_PYTHON=C:\\路径\\python.exe"
    )


if __name__ == "__main__":
    found = detect_python()
    print(found or missing_python_hint())
    sys.exit(0 if found else 1)
