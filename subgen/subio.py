"""字幕文件读写（供校对工作台使用）。

为什么要单独一个模块：工作台需要**读回已生成的字幕**再编辑保存，
而 formats.py 只负责"从 cue 生成文本"。这里补上反向解析，
并做了容错 —— 用户手上可能有各种来源的 srt（自己改过、别的工具生成的），
不能假定格式规整。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# 时间轴行：00:00:01,234 --> 00:00:03,456（也兼容 VTT 的 . 分隔与无小时格式）
_TS = r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
_TS_SHORT = r"(\d{1,2}):(\d{2})[.,](\d{1,3})"
_LINE = re.compile(rf"^\s*({_TS}|{_TS_SHORT})\s*-->\s*({_TS}|{_TS_SHORT})")


def parse_ts(s: str) -> float:
    """把时间戳字符串转成秒。支持 时:分:秒,毫秒 与 分:秒.毫秒 两种写法。"""
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
    except ValueError:
        pass
    return 0.0


def fmt_ts(seconds: float) -> str:
    """秒 -> SRT 时间戳（00:00:01,234）。"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@dataclass
class SubCue:
    """一条字幕。index 保留原序号，便于回写时对齐。"""

    start: float
    end: float
    text: str
    index: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }


def parse_srt(text: str) -> List[SubCue]:
    """解析 SRT / VTT 内容。容错优先，尽量不因格式小瑕疵整体失败。"""
    cues: List[SubCue] = []
    if not text:
        return cues

    # 去掉 BOM 与 VTT 头
    text = text.lstrip("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    i = 0
    n = len(lines)
    idx = 0
    while i < n:
        line = lines[i].strip()
        if not line or line.upper().startswith("WEBVTT") or line.isdigit() is False and "-->" not in line:
            i += 1
            continue
        if "-->" not in line:
            i += 1
            continue

        m = _LINE.match(line)
        if not m:
            i += 1
            continue

        # 时间轴行前面可能是序号行
        a = line.split("-->")[0].strip()
        b = line.split("-->")[1].strip().split()[0] if "-->" in line else ""
        start, end = parse_ts(a), parse_ts(b)

        # 收集文本行，直到空行
        i += 1
        body: List[str] = []
        while i < n and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        idx += 1
        cues.append(SubCue(start=start, end=end, text="\n".join(body), index=idx))

    return cues


def to_srt(cues: Sequence[SubCue]) -> str:
    """生成 SRT 文本。"""
    out: List[str] = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{fmt_ts(c.start)} --> {fmt_ts(c.end)}")
        out.append((c.text or "").strip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def read_subtitle(path: str) -> List[SubCue]:
    """读字幕文件，自动探测编码（SRT 常见 UTF-8 BOM / GBK）。"""
    if not path or not os.path.isfile(path):
        return []
    raw = open(path, "rb").read()
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", errors="replace")
    return parse_srt(text)


def write_subtitle(path: str, cues: Sequence[SubCue]) -> str:
    """写回 SRT。用 UTF-8 BOM 保证老播放器不乱码（与主流程保持一致）。"""
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(to_srt(cues))
    return path


def find_subtitles(video_path: str) -> List[str]:
    """找出与某个视频同名的字幕文件（用于自动载入）。

    支持 视频.mp4 -> 视频.srt / 视频.zh.srt / 视频.en.srt / 视频.zh-en.bilingual.srt
    """
    base = os.path.splitext(video_path)[0]
    d = os.path.dirname(video_path)
    stem = os.path.basename(base)
    if not os.path.isdir(d):
        return []
    hits = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith((".srt", ".vtt")):
            continue
        if fn == stem + os.path.splitext(fn)[1] or fn.startswith(stem + "."):
            hits.append(os.path.join(d, fn))
    return hits
