"""音频波形提取（供时间轴绘制）。

为什么要自己提：时间轴要画波形，浏览器不能直接从视频文件读采样。
两条路：
  1. Web Audio 解码整个文件 —— 长视频要几十秒，且占内存
  2. ffmpeg 降采样成低采样率单声道 PCM，算峰值 —— 34 秒视频 0.4 秒搞定

选 2。同时做**分块提取**：时间轴只显示可视范围，按需取那一段，
长音频也不会卡。数据以「每点最大振幅」形式给前端，量很小
（100 点/秒，10 分钟音频也只有 60 KB）。
"""

from __future__ import annotations

import array
import os
import subprocess
import sys
from typing import List, Optional, Tuple

from .audio import FFmpeg, FFmpegNotFound

# 波形的默认分辨率：每秒多少采样点。
# 100 点/秒 在缩放到 10 秒窗口时仍有 10 px/点的精度，足够看清单字间隙。
PPS = 100
SAMPLE_RATE = 8000     # 只用来算包络，8 kHz 足够


def extract_peaks(
    media_path: str,
    start: float = 0.0,
    duration: Optional[float] = None,
    *,
    pps: int = PPS,
    ff: Optional[FFmpeg] = None,
) -> List[int]:
    """提取 [start, start+duration) 区间的波形峰值（0~32767）。

    返回的每个整数代表一小段音频的振幅绝对值最大值，
    前端按这个数组画对称波形。
    """
    try:
        ff = ff or FFmpeg()
    except FFmpegNotFound:
        return []

    cmd = [ff.ffmpeg, "-hide_banner", "-loglevel", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", media_path]
    if duration and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-vn", "-map", "0:a:0",       # 没有音轨时失败，由调用方兜底
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-",
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0 or not r.stdout:
        return []

    pcm = r.stdout
    arr = array.array("h")
    arr.frombytes(pcm[: len(pcm) // 2 * 2])
    total = len(arr)
    if total == 0:
        return []

    # 点数按实际音频长度算，保证时间轴上的位置准确
    actual_dur = duration if (duration and duration > 0) else (total / SAMPLE_RATE)
    n_pts = max(1, int(round(actual_dur * pps)))
    step = total / n_pts

    peaks: List[int] = []
    for k in range(n_pts):
        a = int(k * step)
        b = min(total, int((k + 1) * step))
        if b <= a:
            b = min(total, a + 1)
        if b <= a:
            peaks.append(0)
            continue
        seg = arr[a:b]
        # max(seg) 与 -min(seg) 取大者 —— 直接用 abs 会漏掉整段负半周的情况
        peaks.append(int(max(max(seg), -min(seg))))

    return peaks


def peaks_summary(peaks: List[int]) -> Tuple[int, int]:
    """给出一段波形的 (峰值, 静音阈值)，便于前端标出静音区。"""
    if not peaks:
        return 0, 0
    mx = max(peaks)
    return mx, int(mx * 0.06)
