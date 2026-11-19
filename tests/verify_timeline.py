"""验证时间轴 JS 无运行时错误，并检查核心绘图逻辑。

用 Edge 无头加载带 mock 的工作台，捕获所有 JS 异常与控制台错误。
无头环境最容易暴露的问题：元素 id 拼错、函数未定义、时序问题。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

ERROR_HOOK = """
<script>
window.__errs = [];
window.addEventListener('error', (e) => {
  window.__errs.push('ERROR: ' + (e.message || '') + ' @ ' + (e.filename||'') + ':' + (e.lineno||0));
});
window.addEventListener('unhandledrejection', (e) => {
  window.__errs.push('PROMISE: ' + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
});
const _ce = console.error;
console.error = function(){ window.__errs.push('CONSOLE: ' + Array.from(arguments).join(' ')); _ce.apply(console, arguments); };
</script>
"""

CHECKS = """
<script>
setTimeout(() => {
  const R = {};
  try {
    // 核心元素是否存在
    ['tlScroll','tlInner','tlRuler','tlCues','tlWave','tlCanvas','tlHead',
     'tlUndo','tlRedo','tlSplit','tlMerge','tlZoom','tlInfo'].forEach(id => {
      R['el_' + id] = !!document.getElementById(id);
    });
    // 时间轴是否真的画出了东西
    R['cue_bars'] = document.querySelectorAll('#tlCues .tl-cue').length;
    R['ruler_ticks'] = document.querySelectorAll('#tlRuler .tl-tick').length;
    R['inner_width'] = document.getElementById('tlInner').style.width;
    R['canvas_width'] = document.getElementById('tlCanvas').width;
    R['head_left'] = document.getElementById('tlHead').style.left;
    R['cue_widths'] = Array.from(document.querySelectorAll('#tlCues .tl-cue'))
        .slice(0,4).map(e => e.style.left + '/' + e.style.width);

    // 核心函数是否存在
    ['tlRender','tlRenderWave','tlRenderCues','tlUndo','tlRedo','tlBind',
     'tlLoadWave','tlUpdateHead'].forEach(fn => { R['fn_' + fn] = typeof window[fn]; });

    // 交互：模拟一次撤销
    if (typeof window.tlPushHist === 'function' && typeof window.tlUndo === 'function'){
      const before = S.cues.length;
      window.tlPushHist();
      R['hist_after_push'] = TL.hist.length;
      window.tlUndo();
      R['undo_ok'] = S.cues.length === before;
    }

    // 时间轴坐标换算：pxps 变化后宽度应成比例
    if (typeof window.tlRender === 'function'){
      TL.pxps = 60;
      window.tlRender();
      const w1 = parseFloat(document.getElementById('tlInner').style.width);
      TL.pxps = 120;
      window.tlRender();
      const w2 = parseFloat(document.getElementById('tlInner').style.width);
      R['zoom_scales'] = w2 > w1 * 1.6;
      R['w1'] = w1; R['w2'] = w2;
      TL.pxps = 60; window.tlRender();
    }

    R['errors'] = window.__errs || [];
  } catch(e){
    R['fatal'] = e.message + ' | ' + (e.stack||'').split('\\n')[1];
    R['errors'] = window.__errs || [];
  }
  document.title = 'RESULT:' + JSON.stringify(R);
}, 2600);
</script>
"""


def main() -> int:
    src = os.path.join(APP, "webui", "review.html")
    html = open(src, encoding="utf-8").read()

    cues = [
        {"index": 1, "start": 0.02, "end": 3.36, "text": "第一句测试内容"},
        {"index": 2, "start": 3.36, "end": 6.10, "text": "第二句测试内容"},
        {"index": 3, "start": 6.10, "end": 9.40, "text": "第三句测试内容"},
        {"index": 4, "start": 9.40, "end": 12.2, "text": "第四句测试内容"},
    ]
    wave = [0] * 1400
    for i in range(len(wave)):
        t = i / 100
        if (t > 0.3 and t < 3.2) or (t > 3.6 and t < 9.0):
            wave[i] = int(32767 * (0.5 + 0.5 * abs(__import__("math").sin(i * 0.05))))
        else:
            wave[i] = 300

    mock = """
<script>
window.pywebview = { api: {
  review_init: async () => ({
    videoPath: 'C:\\\\t\\\\demo.mp4',
    videoUrl: '',
    subPath: 'C:\\\\t\\\\demo.srt',
    mediaType: 'video',
  }),
  load_subtitle: async () => ({ path:'demo.srt', cues: __CUES__ }),
  get_waveform: async () => (__WAVE__),
  pick_subtitle: async () => null,
  open_video: async () => null,
  save_subtitle: async () => ({ok:true, path:'demo.srt'}),
  save_as: async () => ({ok:true}),
  reveal: async () => ({ok:true}),
}};
setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 30);
</script>
"""
    mock = mock.replace("__CUES__", json.dumps(cues, ensure_ascii=False))
    mock = mock.replace("__WAVE__", json.dumps(
        {"ok": True, "start": 0, "duration": 14, "pps": 100, "peak": 32767, "peaks": wave}))

    page = os.path.join(APP, "out", "_tltest.html")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(html.replace("</head>", ERROR_HOOK + "</head>")
                    .replace("</body>", mock + CHECKS + "</body>"))

    p = page.replace("\\", "/")
    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--virtual-time-budget=6000",
         "--dump-dom", f"file:///{p}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    dom = r.stdout or ""

    m = re.search(r"<title>RESULT:(.*?)</title>", dom, re.S)
    if not m:
        print("未能取到结果（页面可能加载失败）")
        print(dom[:600])
        return 1

    data = json.loads(m.group(1))
    fails = []

    def chk(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
        if not cond:
            fails.append(name)

    print("=" * 62)
    print("时间轴编辑器 · 结构与交互验证")
    print("=" * 62)

    print("\n[1] 无 JS 错误")
    errs = [e for e in (data.get("errors") or []) if "ResizeObserver" not in e]
    chk("无运行时异常", not errs, str(errs[:3]))
    chk("无致命错误", "fatal" not in data, str(data.get("fatal", "")))

    print("\n[2] 元素与函数")
    el_bad = [k[3:] for k, v in data.items() if k.startswith("el_") and not v]
    chk("时间轴元素齐全", not el_bad, str(el_bad))
    fn_bad = [k[3:] for k, v in data.items()
              if k.startswith("fn_") and v not in ("function",)]
    chk("核心函数已定义", not fn_bad, str(fn_bad))

    print("\n[3] 绘制结果")
    chk("字幕条已绘制", data.get("cue_bars", 0) > 0, f"cue_bars={data.get('cue_bars')}")
    chk("时间刻度已绘制", data.get("ruler_ticks", 0) > 2, f"ticks={data.get('ruler_ticks')}")
    chk("画布已设尺寸", (data.get("canvas_width") or 0) > 10, f"w={data.get('canvas_width')}")
    chk("播放头已定位", (data.get("head_left") or "") != "", str(data.get("head_left")))
    print(f"       字幕条位置: {data.get('cue_widths')}")
    print(f"       时间轴宽度: {data.get('inner_width')}")

    print("\n[4] 交互逻辑")
    chk("撤销栈可用", data.get("hist_after_push", 0) > 0, str(data.get("hist_after_push")))
    chk("撤销恢复正确", data.get("undo_ok") is True, str(data.get("undo_ok")))
    chk("缩放改变宽度", data.get("zoom_scales") is True,
        f"{data.get('w1')} -> {data.get('w2')}")

    print("\n" + "=" * 62)
    if fails:
        print(f"失败 {len(fails)} 项：{', '.join(fails)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
