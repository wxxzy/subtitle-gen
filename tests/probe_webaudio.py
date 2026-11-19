"""验证 Web Audio 能否处理跨源视频音频。

关键问题：界面由 pywebview 的 http_server 提供（端口 A），
媒体由 mediaserve 提供（端口 B），两者**不同源**。
跨源媒体接入 AudioContext 会静默产出静音（不报错，极难排查），
所以必须实测确认 CORS 生效后能否真正取到音频数据。
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subgen.mediaserve import get_server  # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
D = r"C:\workspaces\xhs-video-downloader\downloads\67a4612d000000000d009085"

TEST_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<video id="v" crossorigin="anonymous" src="__URL__" style="width:320px"></video>
<pre id="log" style="font-family:monospace;font-size:12px"></pre>
<script>
const out = [];
const L = (m) => { out.push(m); document.getElementById('log').textContent = out.join('\\n'); };
window.__result = null;

async function run(){
  const v = document.getElementById('v');
  await new Promise(r => { v.addEventListener('loadedmetadata', r, {once:true}); setTimeout(r, 4000); });
  L('视频元数据: duration=' + (v.duration||0).toFixed(2) + 's');

  let ac;
  try {
    ac = new (window.AudioContext || window.webkitAudioContext)();
    L('AudioContext: ' + ac.state + '  sampleRate=' + ac.sampleRate);
  } catch(e){ L('AudioContext 创建失败: ' + e); window.__result = {ok:false, err:String(e)}; return; }

  let src, analyser, data;
  try {
    src = ac.createMediaElementSource(v);
    analyser = ac.createAnalyser();
    analyser.fftSize = 2048;
    src.connect(analyser);
    analyser.connect(ac.destination);
    data = new Uint8Array(analyser.fftSize);
    L('已接入 MediaElementSource（无跨源异常）');
  } catch(e){
    L('接入失败（多半是跨源）: ' + e);
    window.__result = {ok:false, err:String(e)}; return;
  }

  // 播放并读取频谱
  try { v.currentTime = 1.0; await v.play(); } catch(e){ L('播放失败: ' + e); }
  // 采样若干次，看是否有真实音频能量
  let maxLevel = 0, samples = 0, nonzero = 0;
  for (let i=0;i<40;i++){
    await new Promise(r=>setTimeout(r,100));
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (let j=0;j<data.length;j++){ peak = Math.max(peak, Math.abs(data[j]-128)); }
    samples++;
    if (peak > 0) nonzero++;
    maxLevel = Math.max(maxLevel, peak);
    if (i === 10) L('1秒后峰值: ' + peak);
  }
  v.pause();
  L('采样 ' + samples + ' 次，有信号 ' + nonzero + ' 次，最大峰值 ' + maxLevel);
  L(maxLevel > 2 ? '✓ 拿到了真实音频数据（CORS 正常）'
                 : '✗ 音频全零 —— 跨源被静音了');
  window.__result = {ok: maxLevel > 2, maxLevel, nonzero, samples,
                     duration: v.duration, ctxState: ac.state};
}

// 顺带测 BiquadFilter（做均衡器的基础）
setTimeout(async () => {
  try {
    const ac = new (window.AudioContext||window.webkitAudioContext)();
    const bq = ac.createBiquadFilter();
    bq.type = 'peaking'; bq.frequency.value = 1000; bq.gain.value = 6; bq.Q.value = 1;
    const dyn = ac.createDynamicsCompressor();
    L('BiquadFilter 可用: ' + (bq.type === 'peaking') + '  gain=' + bq.gain.value);
    L('DynamicsCompressor 可用: ' + !!dyn.threshold);
    window.__nodes = {biquad:true, compressor:true};
  } catch(e){ L('滤波器创建失败: ' + e); window.__nodes = {biquad:false}; }
}, 200);

run();
</script></body></html>
"""


def main() -> int:
    vids = sorted(glob.glob(os.path.join(D, "*.mp4")))
    if not vids:
        print("找不到测试视频")
        return 1
    video = vids[3]

    srv = get_server()
    url = srv.url_for(video)
    print(f"媒体 URL 端口: {srv.port}")

    html = TEST_HTML.replace("__URL__", url)
    page = os.path.join(APP, "out", "_audiotest.html")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)

    out = os.path.join(APP, "out", "audio_probe.json")
    dom = os.path.join(APP, "out", "_audiodom.html")
    p = page.replace("\\", "/")
    r = subprocess.run(
        [EDGE, "--headless=new", "--autoplay-policy=no-user-gesture-required",
         "--disable-gpu", "--virtual-time-budget=12000", "--dump-dom",
         f"file:///{p}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    with open(dom, "w", encoding="utf-8") as f:
        f.write(r.stdout or "")

    # 从 DOM 里抠出日志
    import re
    logs = re.findall(r"^(视频元数据.*|AudioContext.*|已接入.*|接入失败.*|播放失败.*|"
                      r"\d+秒后峰值.*|采样 .*|✓.*|✗.*|BiquadFilter.*|DynamicsCompressor.*)$",
                      (r.stdout or ""), re.M)
    print("\n=== 浏览器内实测 ===")
    for l in logs:
        print("  " + l.strip())

    txt = r.stdout or ""
    ok_audio = "✓ 拿到了真实音频数据" in txt
    srv.stop()

    print("\n结论:")
    print(f"  Web Audio 可处理本服务音频: {'是' if ok_audio else '否'}")
    return 0 if ok_audio else 1


if __name__ == "__main__":
    raise SystemExit(main())
