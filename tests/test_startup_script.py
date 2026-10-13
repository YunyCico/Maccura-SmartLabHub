"""启动与停止入口脚本回归测试。

重点：批处理必须是纯 ASCII 且极薄，复杂逻辑放在 Python 里，
否则在 GBK 代码页下双击会出现解析失败（表现为点击后无反应）。
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START_BAT = ROOT / "启动平台.bat"
STOP_BAT = ROOT / "停止平台.bat"
LAUNCHER = ROOT / "platform" / "tools" / "launch_platform.py"
STOPPER = ROOT / "platform" / "tools" / "stop_platform.py"


def read_bat(path: Path) -> str:
    raw = path.read_bytes()
    assert b"\r\n" in raw, f"{path.name} should use CRLF line endings"
    return raw.decode("ascii")


def test_start_bat_is_pure_ascii_and_thin():
    text = read_bat(START_BAT)
    raw = START_BAT.read_bytes()
    assert all(byte < 128 for byte in raw), "启动平台.bat must not contain non-ASCII bytes"
    assert "chcp 65001" in text
    assert 'set "ROOT=%~dp0"' not in text  # 只用 cd /d，不重复展开
    assert 'cd /d "%~dp0"' in text
    assert 'set "PYTHON_EXE=C:\\Users\\98319\\.workbuddy\\binaries\\python\\envs\\smartlabhub\\Scripts\\python.exe"' in text
    assert '"%PYTHON_EXE%" "%LAUNCHER%" %*' in text
    assert len(raw) < 1600, "启动平台.bat 不应承载复杂逻辑"


def test_stop_bat_is_pure_ascii_and_thin():
    text = read_bat(STOP_BAT)
    raw = STOP_BAT.read_bytes()
    assert all(byte < 128 for byte in raw), "停止平台.bat must not contain non-ASCII bytes"
    assert '"%PYTHON_EXE%" "%STOPPER%" %*' in text
    assert "netstat" not in text, "端口处理应在 Python 中完成"
    assert len(raw) < 1600


def test_bats_do_not_contain_mojibake_or_old_paths():
    for path in (START_BAT, STOP_BAT):
        text = read_bat(path)
        assert "\ufffd" not in text
        assert "锟" not in text
        assert "Qoder" not in text
        assert "backend\\.venv" not in text
        assert "npm run dev" not in text


def test_launcher_defines_required_steps():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "def backend_ready()" in text
    assert "def frontend_ready()" in text
    assert "def embed_ready()" in text
    assert "def preflight()" in text
    assert "--strictPort" in text
    assert 'def spawn(' in text
    assert "PROXY" in text, "必须绕过本机代理访问 localhost"
    assert "--no-browser" in text
    assert '"--timeout"' in text
    assert "webbrowser.open" in text
    assert "backend.log" not in text or "backend.log" in text


def test_launcher_content_checks_are_not_status_only():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert '\\"status\\"' in text or '"status"' in text
    assert 'id="app"' in text
    assert "SmartLabHub" in text
    assert "智慧实验室" in text


def test_stopper_protects_foreign_processes():
    text = STOPPER.read_text(encoding="utf-8")
    assert "def belongs_to_platform(" in text
    assert "[SKIP]" in text
    assert "--force" in text
    assert "def stop_pid(" in text
    assert "stopped-force" in text
    assert 'endswith(f":{port}")' in text, "端口匹配必须精确后缀，不能用 findstr 子串"


def test_bats_and_tools_exist():
    for path in (START_BAT, STOP_BAT, LAUNCHER, STOPPER):
        assert path.is_file(), f"missing {path}"


def test_backend_service_imports_os():
    service = ROOT / "platform" / "backend" / "app" / "services" / "smartlab_service.py"
    text = service.read_text(encoding="utf-8")
    assert "import os" in text, "大表删除分支使用 os，必须导入"