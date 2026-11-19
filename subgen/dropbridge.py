"""拖放路径桥接。

背景：pywebview 6.x 没暴露 HTML5 拖放的接口，浏览器沙箱也拿不到本地文件
真实路径（只能拿到文件名）。但 pywebview 的 Windows 后端是 pythonnet 驱动的
WinForms，而 WinForms 有完整的 DragDrop 事件、能拿到真实路径。

做法：找到 pywebview 的主窗体，挂 DragDrop 事件，把路径交给前端。
这是唯一可靠的方案——纯前端做不到。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, List, Optional


def attach(window, on_files: Callable[[List[str]], None]) -> bool:
    """给 pywebview 窗口挂拖放事件。

    on_files: 收到真实文件路径列表时回调
    返回是否挂接成功。
    """
    try:
        import clr  # type: ignore
        clr.AddReference("System.Windows.Forms")
        import System.Windows.Forms as WinForms  # type: ignore
    except Exception:
        return False

    state = {"form": None, "tries": 0}

    def find_form():
        """pywebview 的窗体在 webview.start() 之后才创建，需要轮询等待。"""
        for _ in range(60):  # 最多等约 12 秒
            try:
                forms = WinForms.Application.OpenForms
                for i in range(forms.Count):
                    f = forms[i]
                    # pywebview 的窗体标题就是 create_window 时给的名字
                    try:
                        title = f.Text or ""
                    except Exception:
                        title = ""
                    if title and "subgen" in title.lower():
                        return f
                # 退而求其次：取第一个有 WebView 的窗体
                for i in range(forms.Count):
                    f = forms[i]
                    try:
                        if f.Controls.Count > 0:
                            return f
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(0.2)
        return None

    def hook(form) -> None:
        # WinForms 的 DragDropEffects 有 None / Copy 成员，
        # 但 None 是 Python 关键字不能写 .None，必须用 getattr 取。
        Effects = WinForms.DragDropEffects
        EFFECT_NONE = getattr(Effects, "None")
        EFFECT_COPY = getattr(Effects, "Copy")

        def on_drag_enter(sender, e):
            try:
                if e.Data.GetDataPresent(WinForms.DataFormats.FileDrop):
                    e.Effect = EFFECT_COPY
                else:
                    e.Effect = EFFECT_NONE
            except Exception:
                pass

        def on_drag_drop(sender, e):
            try:
                if not e.Data.GetDataPresent(WinForms.DataFormats.FileDrop):
                    return
                items = e.Data.GetData(WinForms.DataFormats.FileDrop)
                paths: List[str] = []
                for it in items:
                    p = str(it)
                    if os.path.isfile(p):
                        paths.append(p)
                    elif os.path.isdir(p):
                        # 拖入文件夹时展开其中的媒体文件
                        for name in sorted(os.listdir(p)):
                            fp = os.path.join(p, name)
                            if os.path.isfile(fp):
                                paths.append(fp)
                if paths:
                    on_files(paths)
            except Exception:
                pass

        form.AllowDrop = True
        form.add_DragEnter(on_drag_enter)
        form.add_DragDrop(on_drag_drop)

    def worker():
        form = find_form()
        if form is not None:
            try:
                hook(form)
                state["form"] = form
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True, name="drop-hook").start()
    return True


def attach_via_webview(window, on_files: Callable[[List[str]], None]) -> None:
    """备用方案：若拿不到 WinForms，则用 JS 侧兜底（只能拿到文件名）。

    前端已实现 resolve_dropped：按文件名在桌面/下载等常见目录里查找。
    这里不额外做事情，只为保持接口一致。
    """
    return None
