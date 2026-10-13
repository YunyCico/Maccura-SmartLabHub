"""SmartLabHub 工作平台停止器。

关闭由 launch_platform.py 启动的后端与前端进程，并可选清理 logs 目录。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # 项目根（platform/tools -> platform -> 项目根）
LOG_DIR = ROOT / "logs"

PORTS = (8000, 5173, 8765)


def log(message: str) -> None:
    print(message, flush=True)


def listening_pids(port: int) -> list[int]:
    if os.name != "nt":
        return []
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        ).stdout
    except Exception as error:
        log(f"  [WARN] netstat failed: {error}")
        return []

    pids: list[int] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1]
        if not local.endswith(f":{port}"):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def process_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=20,
        ).stdout.decode("gbk", "replace")
    except Exception:
        return ""
    for line in output.splitlines():
        parts = [item.strip('" ') for item in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) == pid:
            return parts[0]
    return ""


def command_line(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
            ],
            capture_output=True,
            timeout=30,
        )
        output = completed.stdout.decode("utf-8", "replace").strip()
        if output:
            return output
        return completed.stderr.decode("gbk", "replace").strip()
    except Exception:
        return ""


def belongs_to_platform(pid: int) -> bool:
    name = process_name(pid).lower()
    if not any(token in name for token in ("python", "node", "uvicorn", "vite")):
        return False
    line = command_line(pid)
    if not line:
        # 无法确认归属时，只对明确的运行时进程放行
        return name in ("python.exe", "node.exe")
    return "smartlabhub" in line.lower() or "工作平台" in line


def terminate(pid: int, graceful: bool = True) -> bool:
    if os.name == "nt":
        args = ["taskkill", "/PID", str(pid)]
        if graceful:
            args.append("/T")
        else:
            args.extend(["/T", "/F"])
        try:
            # taskkill 输出为本地代码页（GBK），强制用 UTF-8 会抛解码错误
            result = subprocess.run(
                args, capture_output=True, encoding="utf-8", errors="replace", timeout=20
            )
            return result.returncode == 0
        except Exception as error:
            log(f"  [WARN] taskkill failed for {pid}: {error}")
            return False
    try:
        os.kill(pid, signal.SIGTERM if graceful else signal.SIGKILL)
        return True
    except Exception as error:
        log(f"  [WARN] kill failed for {pid}: {error}")
        return False


def pid_alive(pid: int, wait: float = 3.0) -> bool:
    """温和终止后等待进程真正消失。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        if pid not in listening_pids_all():
            return True
        time.sleep(0.25)
    return pid not in listening_pids_all()


def listening_pids_all() -> set[int]:
    pids: set[int] = set()
    for port in PORTS:
        pids.update(listening_pids(port))
    return pids


def stop_pid(pid: int) -> str:
    """先温和终止，超时后升级强杀。返回 stopped / stopped-force / failed。"""
    if terminate(pid, graceful=True) and pid_alive(pid):
        return "stopped"
    if terminate(pid, graceful=False) and pid_alive(pid):
        return "stopped-force"
    return "failed"


def port_released(port: int, wait: float = 1.5) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        if not listening_pids(port):
            return True
        time.sleep(0.25)
    return not listening_pids(port)


def clear_logs() -> None:
    if not LOG_DIR.is_dir():
        return
    for item in LOG_DIR.glob("*.log"):
        try:
            item.unlink()
            log(f"  removed log: {item.name}")
        except Exception as error:
            log(f"  [WARN] cannot remove {item.name}: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop the SmartLabHub platform")
    parser.add_argument("--force", action="store_true", help="skip the ownership check")
    parser.add_argument("--clear-logs", action="store_true", help="delete logs/*.log")
    args = parser.parse_args()

    log("=" * 62)
    log("SmartLabHub Work Platform Stopper")
    log(f"root: {ROOT}")
    log("=" * 62)

    stopped = 0
    skipped = 0
    handled: set[int] = set()

    for port in PORTS:
        pids = listening_pids(port)
        if not pids:
            log(f"  [OK] port {port} is free")
            continue
        for pid in pids:
            if pid in handled:
                continue
            handled.add(pid)
            name = process_name(pid) or "unknown"
            if not args.force and not belongs_to_platform(pid):
                log(f"  [SKIP] port {port} pid {pid} ({name}) is not a platform process")
                log("         use --force to stop it anyway")
                skipped += 1
                continue
            outcome = stop_pid(pid)
            if outcome == "stopped":
                log(f"  [STOPPED] port {port} pid {pid} ({name})")
                stopped += 1
            elif outcome == "stopped-force":
                log(f"  [STOPPED-FORCE] port {port} pid {pid} ({name})")
                stopped += 1
            else:
                log(f"  [WARN] could not stop pid {pid} on port {port}")
                skipped += 1

    time.sleep(1.0)
    remaining = {port: listening_pids(port) for port in PORTS}
    for port, pids in remaining.items():
        if pids:
            log(f"  [WARN] port {port} still listening: {pids}")
        else:
            log(f"  [OK] port {port} released")

    if args.clear_logs:
        log("clearing logs:")
        clear_logs()

    log("-" * 62)
    log(f"stopped: {stopped}, skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())