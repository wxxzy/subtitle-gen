@echo off
chcp 65001 >nul
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

REM subgen 图形界面启动器
if not exist ".venv\Scripts\pythonw.exe" (
  echo [X] 未找到运行环境，请先双击 install.bat 完成安装
  pause & exit /b 1
)

REM 用 pythonw 启动，不弹黑色控制台窗口
start "" ".venv\Scripts\pythonw.exe" -m subgen.gui_main

REM 若上面失败（例如没装界面依赖），用 python 显示错误信息
timeout /t 2 /nobreak >nul
tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | find /I "pythonw.exe" >nul
if errorlevel 1 (
  echo [!] 界面未能启动，正在显示诊断信息...
  echo.
  ".venv\Scripts\python.exe" -m subgen.gui_main
  echo.
  pause
)
endlocal
