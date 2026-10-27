"""打包给同事用的发布包。

做的事：
1. 复制必要文件到目标目录（排除开发垃圾）
2. 构建前端产物 home/dist（发布模式由后端直接托管）
3. 写一份《给同事的部署说明.md》

用法：
    python platform/tools/package_release.py [目标目录]

默认目标：项目同级目录下的 SmartLabHub-发布包
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 整个目录排除
EXCLUDE_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".git", ".idea", ".vscode", "logs", "backup", "_cdp_release",
}
# 单个文件排除
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".old", ".bak"}
# 顶层只带这些（其余开发目录不带）
TOP_LEVEL_INCLUDE = {
    "home", "modules", "platform", "db", "deployment", "tests",
    "README.md", "启动平台.bat", "停止平台.bat", "启动平台-发布版.bat",
    "给同事的部署说明.md",
}

# home 目录里只带这些
HOME_INCLUDE = {"src", "public", "dist", "index.html", "package.json",
                "package-lock.json", "vite.config.ts", "tsconfig.json"}


def _ignore_for(directory: str, names: list[str]) -> set[str]:
    """shutil.copytree 的 ignore 回调。"""
    ignored: set[str] = set()
    base = Path(directory).name
    for name in names:
        path = Path(directory) / name
        if path.is_dir():
            if name in EXCLUDE_DIRS:
                ignored.add(name)
                continue
            if base == "home" and name not in HOME_INCLUDE:
                ignored.add(name)
        else:
            if path.suffix.lower() in EXCLUDE_SUFFIX:
                ignored.add(name)
    return ignored


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        ignore=_ignore_for,
        dirs_exist_ok=True,
    )


def build_frontend() -> bool:
    """构建前端产物。Node 不可用就跳过（用已有的 dist）。"""
    home = ROOT / "home"
    vite = home / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.is_file():
        print("[跳过] 没装前端依赖（home/node_modules 缺失），沿用现有 dist")
        return (home / "dist" / "index.html").is_file()
    node = shutil.which("node")
    if not node:
        print("[跳过] 没找到 node，沿用现有 dist")
        return (home / "dist" / "index.html").is_file()
    print("[构建] 前端产物 home/dist ...")
    result = subprocess.run(
        [node, str(vite), "build"], cwd=str(home), capture_output=True, text=True
    )
    ok = (home / "dist" / "index.html").is_file()
    print("  " + ("构建成功" if ok else "构建失败"))
    if not ok:
        print(result.stdout[-1500:])
        print(result.stderr[-1500:])
    return ok


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "SmartLabHub-发布包"
    print(f"[打包] 源目录 : {ROOT}")
    print(f"[打包] 目标目录: {target}")

    if target.exists():
        print(f"[打包] 目标已存在，先清空：{target}")
        try:
            shutil.rmtree(target)
        except OSError as error:
            # 清不掉常见两种原因：
            #   ① 上一次跑起来的平台还占着里面的文件；
            #   ② 里面有几千个文件（如 .venv），批量删除被安全机制拦下。
            # 两种都不该中断打包 —— 换个带时间戳的目录继续。
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            fallback = target.with_name(f"{target.name}-{stamp}")
            print(f"[打包] 清空失败（{type(error).__name__}），改用新目录：{fallback.name}")
            print("       旧目录请手动删除，或先双击「停止平台.bat」释放占用。")
            target = fallback
    target.mkdir(parents=True, exist_ok=True)

    for name in sorted(TOP_LEVEL_INCLUDE):
        source = ROOT / name
        if not source.exists():
            print(f"  [跳过] {name}（不存在）")
            continue
        if source.is_dir():
            print(f"  [复制] {name}/")
            copy_tree(source, target / name)
        else:
            print(f"  [复制] {name}")
            shutil.copy2(source, target / name)

    if not build_frontend():
        print("[错误] 前端产物缺失，发布包无法运行。请先在 home/ 下执行 npm install && npm run build")
        return 2

    # 发布模式下同事不需要 node_modules
    nm = target / "home" / "node_modules"
    if nm.exists():
        shutil.rmtree(nm)
        print("  [清理] 移除 home/node_modules（发布模式不需要 Node）")

    # 本机 venv 不可跨机器复用
    venv = target / "platform" / "backend" / ".venv"
    if venv.exists():
        shutil.rmtree(venv)
        print("  [清理] 移除 .venv（同事首次运行会自动重建）")

    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"[完成] 发布包：{target}")
    print(f"       体积：{total / 1024 / 1024:.1f} MB")
    print("       把这个目录整体压缩发给同事即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
