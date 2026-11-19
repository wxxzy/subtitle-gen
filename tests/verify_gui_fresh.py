"""验证 GUI 启动时加载的是**最新**的界面文件。

━━━ 为什么需要这个测试（真实事故）━━━

WebView2 会对本地 http 服务返回的页面做**强缓存**，且不理会 pywebview
发出的 no-cache 头。后果是：**界面文件更新了，重启程序看到的还是旧界面**，
用户会以为"改动没生效"或"需要编译"。

实测证据（同一份 index.html、同样启动参数）：
    不带版本号 -> 旧版 66235 字符，新增控件全部查不到
    带版本号   -> 新版 73538 字符，新增控件正常

修复方式是给 URL 加文件修改时间做版本号。这个测试就是为了
**保证这个修复不会在未来被改回去**——它太容易被忽视，
而一旦出问题，现象是"界面没更新"，排查方向容易跑偏到"要不要编译"上。

跑法：python tests/verify_gui_fresh.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

# 主界面里应该存在的关键控件（覆盖各次迭代新增的功能）
MAIN_UI_KEYS = {
    "sModel":       "模型选择",
    "sProvider":    "大模型服务商下拉",
    "sApiKey":      "API Key 输入",
    "sApiBase":     "接口地址输入",
    "sLlmModel":    "模型名输入",
    "btnTestLlm":   "测试连接按钮",
    "sDesub":       "去原字幕开关",
    "sSimplify":    "繁转简开关",
    "sFixups":      "纠错表",
    "btnReview":    "校对字幕按钮",
}

PROBE = '''
import os, sys, json, threading, time
sys.path.insert(0, r"__APP__")
import webview
from subgen.gui import GuiApi
from subgen.gui_main import versioned_url, HTML_FILE

RESULT = r"__RESULT__"
KEYS = __KEYS__

api = GuiApi()
win = webview.create_window("probe", versioned_url(HTML_FILE), js_api=api,
                            width=1000, height=640)

def driver():
    # 轮询等待页面就绪。固定 sleep 不稳：机器忙时页面还没加载完，
    # evaluate_js 会返回 None，导致误报"加载了旧界面"。
    # 判据是 document.readyState 且关键元素已出现。
    props = ["'%s': !!document.querySelector('#%s')" % (k, k) for k in KEYS]
    props.append("__len: document.documentElement.outerHTML.length")
    props.append("__ready: document.readyState")
    js = "JSON.stringify({" + ", ".join(props) + "})"
    res = {"url": versioned_url(HTML_FILE)[-46:]}
    last = None
    for _ in range(50):                 # 最多等 20 秒
        time.sleep(0.4)
        try:
            r = win.evaluate_js(js)
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            continue
        if not r:
            continue
        try:
            d = json.loads(r) if isinstance(r, str) else None
        except Exception:
            d = None
        if not d:
            continue
        last = d
        # 已就绪且至少看到一个控件 -> 可以收工
        if d.get("__ready") == "complete" and any(
                d.get(k) for k in KEYS):
            break
    res["dom"] = last if isinstance(last, dict) else {"raw": last}
    open(RESULT, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False))
    os._exit(0)

threading.Thread(target=driver, daemon=True).start()
webview.start(debug=False, http_server=True, private_mode=True)
'''


def main() -> int:
    from subgen.gui_main import HTML_FILE, versioned_url

    fails = []

    def chk(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
        if not cond:
            fails.append(name)

    print("=" * 62)
    print("GUI 加载最新界面 · 验证")
    print("=" * 62)

    # ---------- ① 版本号机制本身 ----------
    print("\n[1] 版本号机制")
    url = versioned_url(HTML_FILE)
    chk("URL 带版本号", "?v=" in url, url[-50:])
    m = re.search(r"\?v=(\d+)", url)
    chk("版本号是文件修改时间",
        bool(m) and abs(int(m.group(1)) - int(os.path.getmtime(HTML_FILE))) < 5,
        str(m.group(1) if m else None))
    # 文件改动后版本号必须变，否则缓存还是不会失效
    tmp = HTML_FILE + ".probe"
    try:
        with open(HTML_FILE, encoding="utf-8") as f:
            content = f.read()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.utime(tmp, (time.time() + 60, time.time() + 60))
        chk("文件更新后版本号随之变化",
            versioned_url(tmp) != versioned_url(HTML_FILE))
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)

    # ---------- ② 磁盘文件含所有新控件 ----------
    print("\n[2] 磁盘文件内容")
    html = open(HTML_FILE, encoding="utf-8").read()
    missing = [f"{v}({k})" for k, v in MAIN_UI_KEYS.items() if f'id="{k}"' not in html]
    chk("关键控件都在源码里", not missing, str(missing))
    print(f"       源码 {len(html):,} 字符")

    # ---------- ③ 真实启动，检查渲染结果 ----------
    print("\n[3] 真实启动后的渲染结果")
    tmpd = tempfile.mkdtemp(prefix="gui_fresh_")
    result = os.path.join(tmpd, "r.json")
    script = os.path.join(tmpd, "probe.py")
    probe_src = (PROBE.replace("__APP__", APP)
                      .replace("__RESULT__", result)
                      .replace("__KEYS__", repr(list(MAIN_UI_KEYS))))
    with open(script, "w", encoding="utf-8") as f:
        f.write(probe_src)

    try:
        subprocess.run([sys.executable, "-u", script], capture_output=True, timeout=120)
        data = {}
        if os.path.isfile(result):
            data = json.load(open(result, encoding="utf-8"))
        dom = data.get("dom") or {}
        chk("成功拿到渲染结果", bool(dom) and "raw" not in dom, str(data)[:200])
        if dom and "raw" not in dom:
            got_len = dom.get("__len", 0)
            chk("渲染出的是最新版（内容长度合理）",
                got_len > len(html) * 0.9,
                f"渲染 {got_len} vs 源码 {len(html)}（差得多说明加载了旧版）")
            absent = [f"{MAIN_UI_KEYS[k]}({k})" for k in MAIN_UI_KEYS
                      if not dom.get(k)]
            chk("所有关键控件都已渲染", not absent, str(absent))
            print(f"       URL: {data.get('url')}")
            print(f"       渲染 {got_len:,} 字符")
    finally:
        import shutil
        shutil.rmtree(tmpd, ignore_errors=True)

    print("\n" + "=" * 62)
    if fails:
        print(f"失败 {len(fails)} 项：{', '.join(fails)}")
        print("\n提示：若「渲染出的是最新版」失败，说明缓存机制回归了，")
        print("      检查 gui_main.py 是否仍在使用 versioned_url() 加载页面。")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
