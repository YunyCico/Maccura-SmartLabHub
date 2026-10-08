@echo off
title SmartLabHub 平台停止器
for /f "tokens=5" %%p in ('netstat -ano ^| findstr LISTENING ^| findstr ":8000 :5173"') do taskkill /PID %%p /T /F >/dev/null 2>&1
echo 已停止平台服务（后端 8000 / 前端 5173，数据分析界面 8765 随后端一起关闭）。
pause
