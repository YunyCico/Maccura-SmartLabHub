@echo off
title SmartLabHub 平台启动器
cd /d C:\Users\98319\Documents\Qoder\2026-09-28\58727fb4

echo [1/3] 启动后端服务（8000 端口，含数据分析界面）...
start "SmartLabHub-Backend" /min cmd /k "backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000"

echo [2/3] 启动平台前端（5173 端口）...
start "SmartLabHub-Frontend" /min cmd /k "cd frontend && set PATH=C:\Program Files\nodejs;%%PATH%% && npm run dev"

echo [3/3] 等待服务就绪后打开浏览器...
timeout /t 12 /nobreak >/dev/null
start "" "D:\software\QUARK\quark.exe" "http://localhost:5173/"

echo.
echo ============================================================
echo  启动完成！
echo    平台首页   http://localhost:5173/
echo    数据分析   首页点数据分析卡片，或直接打开 http://127.0.0.1:8765/
echo  停止方法：运行同目录的 停止平台.bat，或直接关掉两个黑色窗口。
echo ============================================================
pause
