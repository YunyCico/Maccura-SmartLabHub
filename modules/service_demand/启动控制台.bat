@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   数据中台 · 对外数据拉取控制台
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 正在创建运行环境...
    python -m venv .venv
    if errorlevel 1 (
        echo 创建失败，请确认已安装 Python 3.10+
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt
    echo       依赖安装完成
) else (
    echo [1/2] 运行环境已就绪
)

echo [2/2] 启动服务...
echo.
echo   打开浏览器访问:  http://127.0.0.1:8100
echo   关闭此窗口即停止服务
echo.

start "" http://127.0.0.1:8100
.venv\Scripts\python.exe -m uvicorn service_portal.main:app --host 127.0.0.1 --port 8100 --app-dir backend

pause