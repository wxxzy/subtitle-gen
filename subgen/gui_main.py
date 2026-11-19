"""GUI 启动入口（作为模块运行）。

    python -m subgen.gui

用 pywebview 起一个原生窗口，里面是本地 HTML 界面。
不依赖任何服务器，全程本地运行。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_FILE = os.path.join(APP_DIR, "webui", "index.html")


def versioned_url(path: str) -> str:
    """给页面 URL 加一个基于文件修改时间的版本号。

    ★ 为什么必须这么做（踩过的坑，值得记）★
    WebView2 会对本地 http 服务返回的页面做**强缓存**，且不按 pywebview 发的
    no-cache 头办事。结果是：**界面文件更新了，重启程序看到的还是旧界面**，
    连清浏览器缓存都未必生效，用户会以为"改了没生效"或"需要编译"。

    实测证据（同一份 index.html、同样启动参数）：
        不带版本号 -> 渲染出旧版（66235 字符，新增控件全都查不到）
        带版本号   -> 渲染出新版（73538 字符，新增控件正常）
    加版本号后 URL 随文件内容变化，缓存自然失效，这是最省事也最可靠的解法。
    """
    try:
        v = int(os.path.getmtime(path))
    except OSError:
        return path
    return f"{path}?v={v}"


def _fail(msg: str) -> int:
    """启动失败时给出可操作的提示，而不是丢一堆堆栈。"""
    print("=" * 62, file=sys.stderr)
    print("启动界面失败", file=sys.stderr)
    print("=" * 62, file=sys.stderr)
    print(msg, file=sys.stderr)
    print(file=sys.stderr)
    print("排查建议：", file=sys.stderr)
    print("  1. 确认已安装界面依赖：pip install pywebview", file=sys.stderr)
    print("  2. 确认系统有 WebView2 运行时（Win10/11 通常自带）", file=sys.stderr)
    print("     缺失时到微软官网下载 'WebView2 Runtime' 安装", file=sys.stderr)
    print("  3. 也可以继续用命令行：subgen.bat 视频.mp4", file=sys.stderr)
    return 1


def main() -> int:
    if not os.path.isfile(HTML_FILE):
        return _fail(f"界面文件不存在：{HTML_FILE}")

    try:
        import webview
    except ImportError:
        return _fail("未安装 pywebview。\n请执行：pip install pywebview")

    # 保证能 import 到 subgen 包
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)

    from .gui import GuiApi, set_window
    from .dropbridge import attach as attach_drop

    api = GuiApi()
    window = webview.create_window(
        "subgen · 字幕生成",
        versioned_url(HTML_FILE),
        js_api=api,
        width=1200,
        height=800,
        min_size=(980, 660),
        background_color="#eef1f6",
        text_select=False,
        confirm_close=False,
    )
    set_window(window)

    # 拖放：pywebview 不暴露拖放接口，挂 WinForms 的 DragDrop 事件补上
    def on_dropped(paths):
        api.push_dropped(paths)

    attach_drop(window, on_dropped)

    # 用 http_server 模式：中文路径下比 file:// 更稳，且是本地回环不出网。
    #
    # private_mode=True 是刻意的：关掉 WebView2 的持久化存储，
    # 避免旧页面被长期缓存下来（配合上面的版本号，双保险）。
    # 配置存在 settings.json 里，不依赖浏览器存储，所以不会丢设置。
    try:
        webview.start(debug=False, http_server=True, private_mode=True)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-800:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
