@echo off
REM subgen 便捷入口 —— 把视频/音频拖到这里，或命令行传参
setlocal
set ROOT=%~dp0
"%ROOT%.venv\Scripts\python.exe" -m subgen.cli %*
if errorlevel 1 (
  echo.
  echo [提示] 若报错缺少依赖，请先双击 install.bat 完成安装。
)
endlocal
