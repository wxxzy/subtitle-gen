"""验证 GUI 后台线程能否把进度推给网页。

这是"点击开始后一直显示待处理"这个 bug 的针对性测试：
真实启动 pywebview 窗口，从**后台线程**推送进度事件，
然后在主线程读回 DOM，确认 JS 是否真的收到并渲染。

关键：evaluate_js 必须带 callback（异步路径），否则会与主线程消息泵互锁。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webview  # noqa: E402

from subgen.gui import GuiApi, set_window  # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(APP, "webui", "index.html")
OUT = os.path.join(APP, "out", "thread_test.json")


def main() -> int:
    api = GuiApi()
    w = webview.create_window("thread-test", HTML, js_api=api,
                              width=1200, height=800, hidden=True)
    set_window(w)

    def worker():
        """模拟处理线程：持续推送进度。"""
        time.sleep(2.0)   # 等页面加载完
        for i in range(1, 6):
            api._emit(
                "window.onProgress && window.onProgress("
                + json.dumps({"stage": "asr", "stage_label": "语音识别中…",
                              "current": i * 10, "total": 50,
                              "file_index": 1, "file_count": 1,
                              "file_name": "x.mp4", "elapsed": i},
                             ensure_ascii=False) + ")"
            )
            time.sleep(0.25)
        api._emit("window.onFileDone && window.onFileDone("
                  + json.dumps({"name": "x.mp4", "ok": True, "cues": 9,
                                "outputs": ["C:/t/x.en.srt"]},
                               ensure_ascii=False) + ")")

    def reader():
        time.sleep(4.5)
        res = {}
        try:
            # 这些 evaluate_js 都在主线程之外调用——用 callback 版避免锁死
            res["bar_width"] = w.evaluate_js("document.getElementById('barI').style.width",
                                             lambda v: res.update(_bar=v))
            res["log_count"] = w.evaluate_js(
                "document.querySelectorAll('#logBox .lg').length",
                lambda v: res.update(_log=v))
            res["done_rows"] = w.evaluate_js(
                "document.querySelectorAll('#qList .qrow.done').length",
                lambda v: res.update(_done=v))
            time.sleep(2.0)   # 等异步回调
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"
        try:
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reader, daemon=True).start()
    webview.start(debug=False, http_server=True, private_mode=True,
                  storage_path=os.path.join(APP, "out", "pwvdt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
