@echo off
chcp 65001 >nul
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

echo ============================================================
echo   subgen 环境安装
echo ============================================================
echo.

REM 1. 找 Python
where python >nul 2>nul
if errorlevel 1 (
  echo [X] 未找到 python，请先安装 Python 3.10-3.12 并勾选 Add to PATH
  pause & exit /b 1
)
for /f "delims=" %%i in ('python --version 2^>^&1') do echo [OK] %%i

REM 2. 检查 ffmpeg
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [!] 未找到 ffmpeg，正在尝试用 winget 安装...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  echo [!] 安装完成后请重开本窗口再运行一次
) else (
  echo [OK] ffmpeg 已就绪
)

REM 3. 建虚拟环境
if not exist ".venv\Scripts\python.exe" (
  echo [..] 创建虚拟环境
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo [X] 虚拟环境创建失败
  pause & exit /b 1
)

REM 4. 装依赖（清华源加速）
echo [..] 安装依赖（约 300 MB，首次需要几分钟）
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo [X] 依赖安装失败，请检查网络
  pause & exit /b 1
)

echo.
echo [OK] 安装完成，正在自检...
echo.
".venv\Scripts\python.exe" -m subgen.cli --check
echo.
echo 用法：把视频文件拖到 subgen.bat 上，或命令行执行 subgen.bat 视频.mp4
pause
