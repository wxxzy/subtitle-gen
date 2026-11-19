"""用 Edge headless 验证界面日志功能。

思路：往真实页面里注入一套 mock 的 pywebview API，
然后模拟"点开始 → 进度推进 → 文件完成 → 任务结束"全过程，
最后把日志区的内容导出，检查是否正常记录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

MOCK = r"""
<script>
// === 模拟 pywebview 环境 ===
window.__log = [];
const _lg = (s) => { window.__log.push(s); };

window.pywebview = { api: {
  get_env: async () => ({ffmpeg:true, device:'cuda', gpu_name:'NVIDIA GeForce GTX 1080 Ti', version:'1.1.0'}),
  load_settings: async () => ({model:'turbo',language:'zh',translate:'en',formats:['srt'],
    max_chars:21,max_duration:7,compute_type:'auto',device:'auto',backend:'offline'}),
  save_settings: async () => ({ok:true}),
  probe: async (ps) => ps.map(p=>({path:p,duration:62.2,size:16672527})),
  list_media: async () => [],
  pick_files: async () => [],
  pick_folder: async () => '',
  reveal: async () => ({ok:true}),
  resolve_dropped: async () => ({paths:[], need_dialog:true}),
  start_job: async (payload) => { _lg('start_job: ' + payload.paths.length + ' files'); return {ok:true}; },
  cancel_job: async () => ({ok:true}),
}};

// 自动 boot
setTimeout(() => { window.dispatchEvent(new Event('pywebviewready')); }, 50);

// === 模拟一次完整任务 ===
function fakeRun() {
  // 0) 往队列里塞两个文件（走 addPaths 的真实逻辑）
  addPaths(['C:\\Users\\test\\videos\\demo1.mp4', 'C:\\Users\\test\\videos\\demo2.mp4']);
}

function fakeProgressFlow() {
  const fname = 'demo1.mp4';
  // 1) 文件开始
  window.onFileStart({name: fname, path: 'C:\\Users\\test\\videos\\demo1.mp4', size: 16672527, duration: 62.2});
  // 2) 各阶段
  const stages = [
    {stage:'audio', stage_label:'抽取音轨（16 kHz 单声道）…', current:1, total:1},
    {stage:'asr',   stage_label:'语音识别中…',   current:5,  total:62.2},
    {stage:'asr',   stage_label:'语音识别中…',   current:35, total:62.2},
    {stage:'asr',   stage_label:'语音识别中…',   current:62.2,total:62.2},
    {stage:'translate', stage_label:'翻译 中文 → English…', current:0, total:0},
    {stage:'export',stage_label:'导出字幕文件…', current:0, total:0},
  ];
  stages.forEach((s, i) => {
    setTimeout(() => {
      window.onProgress(Object.assign({file_index:1, file_count:2, file_name:fname, elapsed:i*2}, s));
    }, i * 60);
  });
  // 3) 文件完成
  setTimeout(() => {
    window.onFileDone({
      name: fname, path:'C:\\Users\\test\\videos\\demo1.mp4', ok:true,
      cues: 12, elapsed: 6.2, speed: 10.0,
      outputs: ['C:\\Users\\test\\videos\\demo1.en.srt', 'C:\\Users\\test\\videos\\demo1.zh.srt'],
      preview: [{index:1,start:0,end:2.5,text:'你好',trans:'hello'}],
    });
  }, 420);
  // 4) 第二个文件失败
  setTimeout(() => {
    window.onFileStart({name:'demo2.mp4', path:'C:\\Users\\test\\videos\\demo2.mp4', size: 8000000, duration: 30});
  }, 500);
  setTimeout(() => {
    window.onFileDone({name:'demo2.mp4', path:'C:\\Users\\test\\videos\\demo2.mp4',
                       ok:false, error:'未识别到任何语音内容（可能是纯音乐或静音）'});
  }, 600);
  // 5) 任务结束
  setTimeout(() => {
    window.onJobDone({ok_count:1, fail_count:1, elapsed:8.4, has_preview:true, cancelled:false});
    // 6) 导出结果供检查
    const box = document.getElementById('logBox');
    const rows = Array.from(box.querySelectorAll('.lg')).map(e => ({
      level: (e.className.replace('lg','').trim()),
      text: e.querySelector('.m').textContent,
    }));
    window.__logDump = JSON.stringify({
      count: rows.length,
      cntLabel: document.getElementById('logCnt').textContent,
      rows: rows,
      hasEmptyState: !!box.querySelector('.lg-empty'),
    });
  }, 800);
}
// boot 后自动跑
setTimeout(() => { fakeRun(); setTimeout(fakeProgressFlow, 300); }, 400);
</script>
"""


def main() -> int:
    src = os.path.join(APP, "webui", "index.html")
    with open(src, encoding="utf-8") as f:
        html = f.read()

    test_html = os.path.join(APP, "out", "_logtest.html")
    os.makedirs(os.path.dirname(test_html), exist_ok=True)
    with open(test_html, "w", encoding="utf-8") as f:
        f.write(html.replace("</body>", MOCK + "\n</body>"))

    url = "file:///" + test_html.replace("\\", "/")
    if not os.path.isfile(EDGE):
        print("找不到 Edge，跳过")
        return 1

    # 用 --dump-dom 取渲染后的 DOM（含 JS 执行结果）
    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--virtual-time-budget=4000",
         "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    dom = r.stdout or ""

    # 从 DOM 里提取日志行
    import re
    rows = re.findall(
        r'<div class="lg ([a-z]+)"><span class="t">([^<]+)</span><span class="m">([^<]+)</span></div>',
        dom,
    )
    print("=" * 62)
    print("界面日志功能验证")
    print("=" * 62)
    print(f"抓取到 {len(rows)} 条日志\n")
    for lv, ts, txt in rows:
        mark = {"ok": "✓", "err": "✗", "warn": "!", "stage": "◆", "file": "▶"}.get(lv, "·")
        print(f"  {mark} [{ts}] {txt}")

    cnt = re.search(r'id="logCnt"[^>]*>([^<]*)<', dom)
    print(f"\n计数标签: {cnt.group(1) if cnt else '?'}")

    ok = len(rows) >= 10
    print("\n" + ("✓ 日志功能正常" if ok else "✗ 日志数量不足，可能有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
