"""字幕格式输出：SRT / VTT / ASS / LRC / TXT。"""

from __future__ import annotations

import os
from typing import List, Sequence

from .segmenter import Cue


def fmt_ts(seconds: float, *, sep: str = ",", with_hours: bool = True) -> str:
    """秒 -> 00:00:00,000 形式。"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if with_hours:
        return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"
    return f"{m:02d}:{s:02d}{sep}{ms:03d}"


def fmt_ass_ts(seconds: float) -> str:
    """ASS 用 0:00:00.00（百分秒）。"""
    if seconds < 0:
        seconds = 0.0
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


# ---------------------------------------------------------------- SRT / VTT

def _cue_body(c: Cue) -> str:
    """单语/双语共用的文本块。

    双语时原文在上译文在下——这是行业惯例（观众视线先落在母语）。
    """
    if c.trans_lines:
        return "\n".join(list(c.lines) + list(c.trans_lines))
    return c.text


def to_srt(cues: Sequence[Cue], *, sep: str = ",") -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{fmt_ts(c.start, sep=sep)} --> {fmt_ts(c.end, sep=sep)}")
        out.append(_cue_body(c))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_vtt(cues: Sequence[Cue]) -> str:
    out = ["WEBVTT", ""]
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(
            f"{fmt_ts(c.start, sep='.')} --> {fmt_ts(c.end, sep='.')}"
        )
        out.append(_cue_body(c))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------- ASS

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: {w}
PlayResY: {h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},2,20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 常见中文字体回退顺序（Windows 优先微软雅黑）
DEFAULT_FONTS = ("Microsoft YaHei", "微软雅黑", "Noto Sans CJK SC", "Source Han Sans SC", "Arial")


def to_ass(
    cues: Sequence[Cue],
    *,
    width: int = 1920,
    height: int = 1080,
    font: str = "Microsoft YaHei",
    fontsize: int = 0,
    outline: int = 2,
    shadow: int = 1,
) -> str:
    """生成 ASS。fontsize 为 0 时按画面尺寸自动推算。

    字号必须**同时**受高和宽约束。只按高度算会在竖屏视频上翻车：
    720x1280 的竖屏按高度得 64px，20 个汉字就是 1280px 宽，直接溢出屏幕外。
    所以取「高度换算值」和「宽度约束值」中较小的那个。

    双语用**单事件 + 内联字号覆盖**实现，而不是拆成两条 Event：
      原文（正常字号）\\N {\\fs 小号}译文
    这样两种语言永远紧贴成块，且各条字幕行数不同时也不会错位——
    如果拆成两条 Event 并靠 MarginV 定位，1 行和 2 行译文的字幕
    会呈现不同的堆叠高度，看起来一跳一跳的。
    """
    if fontsize <= 0:
        if width < height:
            # 竖屏：宽度是瓶颈。两侧各留 8%，按每行最多容纳的字数反推字号
            usable = width * 0.84
            per_line_max = 20
            by_width = usable / per_line_max
            by_height = height * 0.045
            fontsize = int(min(by_width, by_height))
        else:
            # 横屏：高度是瓶颈
            fontsize = int(height * 0.05)
        fontsize = max(14, fontsize)

    tsize = max(12, int(fontsize * 0.82))
    margin_v = max(12, int(height * 0.05))

    head = ASS_HEADER.format(
        w=width,
        h=height,
        font=font,
        size=fontsize,
        outline=outline,
        shadow=shadow,
        margin_v=margin_v,
    )

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    # 译文的样式标签：小一号 + 浅黄（&H00D7FFFF 是 ASS 的 BGR 记法）
    trans_tag = f"{{\\fs{tsize}\\c&H00D7FFFF&}}"

    lines = []
    for c in cues:
        body = "\\N".join(esc(l) for l in c.lines)
        if c.trans_lines:
            # 原文在上、译文在下，与 SRT/VTT 的顺序保持一致
            body += "\\N" + trans_tag + "\\N".join(esc(l) for l in c.trans_lines)
        lines.append(
            f"Dialogue: 0,{fmt_ass_ts(c.start)},{fmt_ass_ts(c.end)},Default,,0,0,0,,{body}"
        )
    return head + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- LRC

def to_lrc(cues: Sequence[Cue], *, offset_ms: int = 0) -> str:
    out = []
    for c in cues:
        ms = int(c.start * 1000) + offset_ms
        ms = max(0, ms)
        m, rem = divmod(ms, 60_000)
        s, cs = divmod(rem, 1000)
        tag = f"{m:02d}:{s:02d}.{cs // 10:02d}"
        out.append(f"[{tag}]{' '.join(c.lines)}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------- TXT

def to_txt(cues: Sequence[Cue], *, with_ts: bool = False, paragraphs: bool = True) -> str:
    bilingual = any(c.trans_lines for c in cues)

    if with_ts:
        out = []
        for c in cues:
            ts = fmt_ts(c.start, sep=".")[:8]
            out.append(f"[{ts}] {' '.join(c.lines)}")
            if c.trans_lines:
                out.append(f"[{ts}] {' '.join(c.trans_lines)}")
        return "\n".join(out)

    if not paragraphs:
        out = []
        for c in cues:
            out.append(" ".join(c.lines))
            if c.trans_lines:
                out.append(" ".join(c.trans_lines))
        return "\n".join(out) + "\n"

    # 段落模式：句末标点结尾且停顿较久才换段，便于阅读
    paras: List[str] = []
    buf = ""
    tbuf = ""
    prev_end = None
    for c in cues:
        seg = " ".join(c.lines).strip()
        tseg = " ".join(c.trans_lines).strip() if c.trans_lines else ""
        if not seg and not tseg:
            continue
        if buf and prev_end is not None and (c.start - prev_end) > 1.5:
            paras.append(buf if not tbuf else buf + "\n" + tbuf)
            buf, tbuf = "", ""
        buf = (buf + seg) if buf else seg
        if tseg:
            tbuf = (tbuf + " " + tseg) if tbuf else tseg
        prev_end = c.end
        if seg[-1:] in "。！？!?…":
            paras.append(buf if not tbuf else buf + "\n" + tbuf)
            buf, tbuf = "", ""
    if buf or tbuf:
        paras.append(buf if not tbuf else buf + "\n" + tbuf)
    return "\n\n".join(p for p in paras if p.strip()) + "\n"


# ---------------------------------------------------------------- 派发

WRITERS = {
    "srt": lambda cues, **kw: to_srt(cues, sep=","),
    "vtt": lambda cues, **kw: to_vtt(cues),
    "ass": lambda cues, **kw: to_ass(cues, **kw),
    "lrc": lambda cues, **kw: to_lrc(cues),
    "txt": lambda cues, **kw: to_txt(cues),
    "sami": lambda cues, **kw: to_srt(cues, sep=","),  # 兼容占位
}


def write_subtitle(path: str, cues: Sequence[Cue], fmt: str | None = None, **kw) -> str:
    """写出字幕文件，返回实际路径。fmt 缺省时从扩展名推断。"""
    fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "srt").lower()
    if fmt not in WRITERS:
        raise ValueError(f"不支持的字幕格式：{fmt}（可选 {', '.join(sorted(WRITERS))}）")
    content = WRITERS[fmt](cues, **kw)
    # 字幕统一用 UTF-8：VTT/ASS 现代播放器都正常，SRT 建议加 BOM 兼容老播放器
    encoding = "utf-8-sig" if fmt == "srt" else "utf-8"
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(content)
    return path
