@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set PY=
where python >nul 2>nul && set PY=python
if not defined PY (
  if exist "%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" (
    set PY=%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe
  )
)
if not defined PY (
  echo.
  echo   未找到 Python。请安装 Python 3，或用任意静态服务器打开本目录。
  echo.
  pause
  exit /b 1
)

if not exist "data\programs.js" (
  echo.
  echo   未找到节目单数据，正在采集（首次约需 30 秒）...
  echo.
  "%PY%" tools\collect.py
  if errorlevel 1 (
    echo.
    echo   采集失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo.
echo   二十四时小路电台： http://127.0.0.1:8765/  （已启用播放代理）
echo   浏览器将在 2 秒后自动打开。关闭本窗口即停止服务。
echo.

rem 先起服务，再延时开浏览器，避免浏览器抢在服务之前打开导致连接失败
start "" /min "%PY%" -c "import time,webbrowser; time.sleep(2); webbrowser.open('http://127.0.0.1:8765/')"
"%PY%" tools\serve.py --port 8765 --bind 127.0.0.1

endlocal
