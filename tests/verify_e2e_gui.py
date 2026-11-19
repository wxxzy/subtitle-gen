"""端到端验证：真实启动界面 → 真实处理视频 → 检查事件是否送达。

这是"点击开始后一直待处理"的最终验证。

注意：本脚本**不使用 evaluate_js**（它在后台线程会阻塞）。
改用 pywebview 的 API 通道（poll_events 走的就是这条），天然在 UI 线程。
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
OUT = os.path.join(APP, "out", "e2e_result.json")

VIDEO_DIR = r"C:\workspaces\xhs-video-downloader\downloads\67a4612d000000000d009085"
TEST_VIDEO = os.path.join(
    VIDEO_DIR,
    "20260719_6a5cd01d000000001f01efb2_耗时两年半，终于拍到了野生的妙脆角猫🐱.mp4",
)


def main() -> int:
    api = GuiApi()
    w = webview.create_window("e2e", HTML, js_api=api,
                              width=1280, height=820, background_color="#eef1f6")
    set_window(w)
    result = {"events": [], "event_count": 0}

    def driver():
        # 等页面就绪
        time.sleep(3.5)

        s = {
            "model": "turbo", "language": "zh", "translate": "en",
            "formats": ["srt"], "output": os.path.join(APP, "out", "e2e"),
            "backend": "offline", "device": "auto", "compute_type": "auto",
            "max_chars": 21, "max_duration": 7, "burn": False, "mux": False,
        }
        r = api.start_job({"paths": [TEST_VIDEO], "options": s})
        result["start_job"] = r

        # 轮询事件队列（模拟前端的 poll_events）
        seen = []
        stages = set()
        t0 = time.time()
        while time.time() - t0 < 200:
            evts = api.poll_events()
            for e in evts:
                js = e.get("js", "")
                seen.append(js)
                # 提取事件名与关键字段，便于断言
                for name in ("onFileStart", "onProgress", "onFileDone", "onJobDone", "onLog", "onFatal"):
                    if name + "(" in js:
                        stages.add(name)
            # 结束判断
            if any("onJobDone" in x for x in seen):
                break
            time.sleep(0.15)

        result["event_count"] = len(seen)
        result["event_types"] = sorted(stages)
        # 抓几条样本
        result["sample_progress"] = next((x for x in seen if "onProgress" in x), "")
        result["sample_done"] = next((x for x in seen if "onFileDone" in x), "")
        result["sample_job"] = next((x for x in seen if "onJobDone" in x), "")
        result["tail"] = seen[-6:]

        # 检查产出文件
        outdir = os.path.join(APP, "out", "e2e")
        result["outputs"] = os.listdir(outdir) if os.path.isdir(outdir) else []

        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os._exit(0)

    threading.Thread(target=driver, daemon=True).start()
    webview.start(debug=False, http_server=True, private_mode=True,
                  storage_path=os.path.join(APP, "out", "e2edt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
