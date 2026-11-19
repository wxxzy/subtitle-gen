"""给校对工作台出一个带真实数据的界面预览图。

思路：用真实字幕内容 + 真实的界面 HTML，注入 mock API，
让 Edge 无头模式渲染后截图。这样看到的就是实际效果。
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 用真实字幕内容（桌面那份方言短剧），带一些"已修改"的示例
DEMO_CUES = [
    {"index": 1, "start": 0.02, "end": 3.36, "text": "妈我今天把话给你撂到这儿你别糊"},
    {"index": 2, "start": 3.36, "end": 6.10, "text": "涂嘛我给个孙婉萱人老是八八的有退休金还有"},
    {"index": 3, "start": 6.10, "end": 9.40, "text": "房子你嫁过去住到她那套房子腾出来"},
    {"index": 4, "start": 9.40, "end": 12.20, "text": "给我娃儿当婴儿房天经地义的事情我不同意"},
    {"index": 5, "start": 12.20, "end": 15.00, "text": "我一把年纪了该啥子嫁嘛说出去不让人小调大雅打"},
    {"index": 6, "start": 15.00, "end": 16.10, "text": "打"},
    {"index": 7, "start": 17.30, "end": 19.40, "text": "在这间屋让我娃儿去睡阳台吗"},
    {"index": 8, "start": 21.10, "end": 26.00, "text": "娟儿你倒是说句话呀妈求你你说句话嘛妈"},
    {"index": 9, "start": 26.00, "end": 30.10, "text": "小雯说得对对你就成全我们吧"},
]

MOCK = """
<script>
window.pywebview = { api: {
  review_init: async () => ({
    videoPath: 'C:\\\\Users\\\\wangxingxiang\\\\Desktop\\\\错待婆婆终悔悟-第1集.mp4',
    videoUrl: '',
    subPath: 'C:\\\\Users\\\\wangxingxiang\\\\Desktop\\\\错待婆婆终悔悟-第1集.zh.srt',
    mediaType: 'video',
  }),
  load_subtitle: async () => ({ path: 'x.srt', cues: __CUES__ }),
  pick_subtitle: async () => null,
  open_video: async () => null,
  save_subtitle: async () => ({ok:true, path:'x.srt'}),
  save_as: async () => ({ok:true}),
  reveal: async () => ({ok:true}),
  get_waveform: async () => (window.__WAVE__ || {ok:false}),
}};
setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 40);

// 合成一段波形：模拟"说话-停顿-说话"的节奏，只为预览效果
(function(){
  const peaks = [];
  const N = 3400;          // 34 秒 × 100 pts/s
  for (let i=0;i<N;i++){
    const t = i / 100;
    // 几段"说话区"，中间留静音
    const speaking = (t>0.3&&t<3.2)||(t>3.6&&t<6.0)||(t>6.4&&t<9.3)||
                     (t>9.8&&t<12.1)||(t>12.6&&t<15.0)||(t>15.4&&t<17.0)||
                     (t>17.6&&t<21.0)||(t>21.5&&t<26.0)||(t>26.4&&t<32.0);
    let v = 0;
    if (speaking){
      // 用多个正弦叠加做出语音包络的不规则感
      const env = 0.45 + 0.55*Math.abs(Math.sin(i*0.031)) * (0.6+0.4*Math.abs(Math.sin(i*0.007)));
      const detail = 0.65 + 0.35*Math.abs(Math.sin(i*0.173));
      v = Math.floor(32767 * env * detail);
    } else {
      v = Math.floor(Math.random() * 900);   // 底噪
    }
    peaks.push(Math.min(32767, v));
  }
  window.__WAVE__ = {ok:true, start:0, duration:34, pps:100, peak:32767, peaks:peaks};
})();

// 视频区用一张示意底图代替真实播放（无头模式无法解码视频）
setTimeout(() => {
  const stage = document.getElementById('stage');
  if (stage && !stage.querySelector('.demoimg')) {
    const d = document.createElement('div');
    d.className = 'demoimg';
    d.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;'
      + 'background:linear-gradient(135deg,#1b2233,#0e1118);color:#3d4658;';
    d.innerHTML = '<div style="text-align:center;color:#4a5468">'
      + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" '
      + 'style="width:56px;height:56px;opacity:.6"><rect x="2" y="4" width="20" height="16" rx="2"/>'
      + '<path d="M10 9l5 3-5 3V9z"/></svg>'
      + '<div style="font-size:12.5px;margin-top:10px">错待婆婆终悔悟-第1集.mp4 · 01:31</div>'
      + '<div style="font-size:11px;margin-top:3px;color:#39414f">（预览图中为静态示意，实际运行可正常播放）</div></div>';
    stage.insertBefore(d, stage.firstChild);
  }
  // 造出"已修改"状态以便展示
  const el = document.querySelectorAll('#cueBox .c-text');
  if (el[1]) { el[1].textContent = '涂嘛我给个孙婉萱人老是巴巴的有退休金还有'; 
               el[1].dispatchEvent(new Event('input', {bubbles:true})); }
  if (el[4]) { el[4].textContent = '我一把年纪了该啥子嫁嘛说出去不让人笑掉大牙';
               el[4].dispatchEvent(new Event('input', {bubbles:true})); }
  // 选中第 3 句，展示高亮态
  const rows = document.querySelectorAll('#cueBox .cue');
  if (rows[3]) {
    document.querySelectorAll('#cueBox .cue').forEach(r=>r.classList.remove('cur'));
    rows[3].classList.add('cur');
  }
  // 模拟播放进度
  const bar = document.getElementById('played');
  if (bar) bar.style.width = '38%';
  const head = document.getElementById('head');
  if (head) head.style.left = '38%';
  document.getElementById('tCur').textContent = '00:34.80';
  document.getElementById('tTotal').textContent = '01:31.00';
}, 700);
</script>
"""


# __WAVE_INJECT__
def main() -> int:
    src = os.path.join(APP, "webui", "review.html")
    with open(src, encoding="utf-8") as f:
        html = f.read()

    mock = MOCK.replace("__CUES__", json.dumps(DEMO_CUES, ensure_ascii=False))
    out_html = os.path.join(APP, "out", "_reviewtest.html")
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html.replace("</body>", mock + "\n</body>"))

    if not os.path.isfile(EDGE):
        print("找不到 Edge")
        return 1

    png = os.path.join(APP, "out", "界面校对工作台.png")
    p = out_html.replace("\\", "/")
    subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--window-size=1400,880", "--virtual-time-budget=4000",
         f"--screenshot={png}", f"file:///{p}"],
        capture_output=True, timeout=120,
    )
    ok = os.path.isfile(png)
    print(("已生成: " if ok else "生成失败: ") + png)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
