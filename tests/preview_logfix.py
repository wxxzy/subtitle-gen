"""生成"日志优化后"的界面预览图。

用途：直观确认日志精简效果（对比修复前的 732 条刷屏）。
"""

from __future__ import annotations

import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

ROWS = [
    ("灶边女王（44集）_HEVC-CRF30.mp4", "41:29", "done"),
    ("造火将林，横扫朝堂敌（35集）_HEVC-CRF30.mp4", "36:34", "done"),
    ("错待婆婆终悔悟（25集）_HEVC-CRF30.mp4", "26:51", "done"),
]

LOGS = [
    ("开始处理：3 个文件 · turbo · 中文 · srt · 译成英文（离线）", "stage"),
    ("输出到 …/Desktop/字幕", "info"),
    ("▶ 灶边女王（44集）　41:29 · 39.9 MB", "file"),
    ("✓ 灶边女王（44集）　287 条 · 41.3s · 10.6x　→ …/字幕/灶边女王（44集）.srt", "ok"),
    ("▶ 造火将林，横扫朝堂敌（35集）　36:34 · 35.2 MB", "file"),
    ("✓ 造火将林，横扫朝堂敌（35集）　256 条 · 36.1s · 10.2x　→ …/字幕/造火将林…srt", "ok"),
    ("▶ 错待婆婆终悔悟（25集）　26:51 · 25.8 MB", "file"),
    ("✓ 错待婆婆终悔悟（25集）　198 条 · 27.4s · 9.8x　→ …/字幕/错待婆婆终悔悟（25集）.srt", "ok"),
    ("全部完成：成功 3 个", "ok"),
    ("总用时 2:13", "stage"),
]


def build_mock() -> str:
    rows_js = ",\n".join(
        f"""    ['{n}', '{d}']""" for n, d, _ in ROWS
    )
    logs_js = ",\n".join(
        f"""    [{m!r}, '{lv}']""".replace("'", '"') if '"' not in m
        else f"""    [{m!r}, '{lv}']"""
        for m, lv in LOGS
    )
    return f"""
<script>
window.pywebview = {{ api: {{
  get_env: async () => ({{ffmpeg:true, device:'cuda', gpu_name:'NVIDIA GeForce GTX 1080 Ti', version:'1.1.0'}}),
  load_settings: async () => ({{model:'turbo',language:'zh',translate:'en',formats:['srt'],
    max_chars:21,max_duration:7,compute_type:'auto',device:'auto',backend:'offline',
    output:'C:\\\\Users\\\\wangxingxiang\\\\Desktop\\\\字幕'}}),
  save_settings: async () => ({{ok:true}}),
  probe: async () => [], list_media: async () => [], pick_files: async () => [],
  pick_folder: async () => '', reveal: async () => ({{ok:true}}),
  resolve_dropped: async () => ({{paths:[],need_dialog:true}}),
  start_job: async () => ({{ok:true}}), cancel_job: async () => ({{ok:true}}),
  poll_events: async () => [], detect_subtitle: async () => ({{ok:false}}),
  test_llm: async () => ({{ok:false}}),
  list_llm_providers: async () => ({{ok:true,providers:[],current:{{}}}}),
}}}};
setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 40);

setTimeout(() => {{
  const rows = [{rows_js}];
  document.getElementById('qList').innerHTML = rows.map(([n, d]) =>
    `<div class="qrow done"><div class="handle"></div><div class="q-ico">MP4</div>`
    + `<div class="q-main"><div class="q-name">${{n}}</div>`
    + `<div class="q-sub"><span class="q-tag ok">✓ 已完成</span>`
    + `<span class="sep">·</span>${{d}}</div></div>`
    + `<button class="q-x">×</button></div>`).join('');
  document.getElementById('qCnt').textContent = '3 个文件 · 共 1:44:53';

  const bar = document.getElementById('bar');
  bar.className = 'bar done';
  document.getElementById('barI').style.width = '100%';
  document.getElementById('stText').innerHTML = '全部完成 · <b>3</b> 个文件';
  document.getElementById('stTime').textContent = '总用时 2:13';
  document.getElementById('btnCancel').style.display = 'none';
  document.getElementById('btnOpenOut').style.display = '';

  const logs = [{logs_js}];
  logs.forEach(([m, lv]) => window.logLine(m, lv));
  const c = document.getElementById('logCnt');
  if (c) c.textContent = logs.length + ' 条';
}}, 900);
</script>
"""


def main() -> int:
    src = open(os.path.join(APP, "webui", "index.html"), encoding="utf-8").read()
    page = os.path.join(APP, "out", "_fixui.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(src.replace("</body>", build_mock() + "\n</body>"))

    png = os.path.join(APP, "out", "界面日志优化.png")
    p = page.replace("\\", "/")
    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--window-size=1500,900", "--virtual-time-budget=4500",
         f"--screenshot={png}", f"file:///{p}"],
        capture_output=True, timeout=120,
    )
    ok = os.path.isfile(png)
    print(("已生成: " if ok else "生成失败: ") + png)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
