"""智能断句：把 ASR 输出的 token 流切成适合上屏的字幕行。

为什么需要它：Whisper 的 segment 是按 30 秒窗口切的，经常一句话长达五六十字，
直接当字幕会占满半个屏幕、也无法阅读。本模块负责：

1. 按标点 / 时长 / 长度三重约束切分 cue
2. 双行折行，且不会在行首留标点
3. 中英混排宽度按半角算（中文 2、英文 1），保证视觉行宽一致
4. 修掉过短 cue、cue 间重叠、以及上一句还没消失下一句就来的问题
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

# 句末标点：出现即认为一句话结束，优先在此断句
SENT_END = set("。！？!?…")
# 句中标点：长度已经够了就顺手断开，读起来更舒服
CLAUSE_END = set("，、；：,;:—")
# 不允许出现在行首的字符
NO_LINE_START = set("，。！？、；：,.!?;:）)】」』”\"'…—")


def char_width(ch: str) -> int:
    """单字符显示宽度：全角 2，半角 1。"""
    o = ord(ch)
    if o < 0x1100:
        return 1
    if (
        0x1100 <= o <= 0x115F
        or 0x2E80 <= o <= 0xA4CF
        or 0xAC00 <= o <= 0xD7A3
        or 0xF900 <= o <= 0xFAFF
        or 0xFE30 <= o <= 0xFE6F
        or 0xFF00 <= o <= 0xFF60
        or 0xFFE0 <= o <= 0xFFE6
        or 0x20000 <= o <= 0x3FFFD
    ):
        return 2
    return 1


def text_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def _is_latin(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _needs_space(prev: str, cur: str) -> bool:
    """只在拉丁字母/数字之间补空格，中文之间不补。"""
    if not prev or not cur:
        return False
    if prev[-1].isspace() or cur[0].isspace():
        return False
    return _is_latin(prev[-1]) and _is_latin(cur[0])


def smart_join(tokens: Sequence["Token"]) -> str:
    out = ""
    for t in tokens:
        s = (t.text or "").strip()
        if not s:
            continue
        if _needs_space(out, s):
            out += " "
        out += s
    return out


@dataclass
class Token:
    """一个词 / 字的识别结果。中文场景下常见单字或两三字的粒度。"""

    text: str
    start: float
    end: float
    prob: float = 1.0


@dataclass
class Cue:
    """一条字幕。lines 已按显示宽度折好行。

    trans_lines 存译文，仅在翻译场景填充。之所以放在 Cue 上而不是单独建表，
    是因为时间轴必须与原文严格一一对应——两者拆开存储很容易错位。
    """

    start: float
    end: float
    lines: List[str] = field(default_factory=list)
    tokens: List[Token] = field(default_factory=list)
    trans_lines: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def trans_text(self) -> str:
        return "\n".join(self.trans_lines)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def copy_timing(self, lines: List[str]) -> "Cue":
        """用新文本复制一条 cue，保留时间轴。"""
        return Cue(start=self.start, end=self.end, lines=list(lines))


def _has_space(text: str) -> bool:
    return " " in text or "\t" in text


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c in "'-"


def wrap_lines(text: str, max_width: int = 42, max_lines: int = 2) -> List[str]:
    """折行。返回 1~max_lines 行。

    max_width 单位为半角字符数：42 约等于 21 个汉字。

    断点优先级：句末标点 > 句中标点 > 空格 > 字符边界（仅 CJK 允许）。
    带空格的语言（英/法/德…）强制在空格处断——绝不能把单词劈成两半，
    中文/日文无空格，按标点或字符边界断即可。
    """
    if not text:
        return [""]
    if text_width(text) <= max_width or max_lines <= 1:
        return [text]

    n = len(text)
    spaced = _has_space(text)

    # 1) 在宽度允许范围内，找得分最高的断点
    best_i, best_score = None, -1.0
    for i in range(1, n):
        w1 = text_width(text[:i])
        if w1 > max_width:
            break
        ch = text[i - 1]
        if ch in SENT_END:
            bonus = 40
        elif ch in CLAUSE_END:
            bonus = 28
        elif ch == " ":
            bonus = 20
        elif ch.isspace():
            bonus = 14
        else:
            bonus = 0
        score = w1 + bonus
        if score > best_score:
            best_score, best_i = score, i

    if best_i is None:
        best_i = 1

    pos = best_i

    # 2) 带空格的语言：把断点吸附到最近的单词边界（向后找空格）
    if spaced:
        # 断点落在单词中间时，退到前一个空格处更自然
        if 0 < pos < n and _is_word_char(text[pos - 1]) and _is_word_char(text[pos]):
            sp = text.rfind(" ", 0, pos)
            pos = sp + 1 if sp > 0 else pos

    # 3) 避免行首标点：把断点右移一位
    guard = 0
    while pos < n and guard < n:
        rest = text[pos:].strip()
        if not rest or rest[0] not in NO_LINE_START:
            break
        pos += 1
        guard += 1

    if max_lines == 2:
        # 第二行仍超宽则继续右移，让两行宽度更均衡。
        # 带空格语言按单词整块挪动，避免二次切碎单词。
        guard = 0
        while pos < n and text_width(text[pos:].strip()) > max_width and guard < n:
            if spaced:
                nxt = text.find(" ", pos)
                pos = (nxt + 1) if nxt != -1 else n
            else:
                pos += 1
            guard += 1
        return [text[:pos].strip(), text[pos:].strip()]

    # max_lines > 2：递归折剩下的部分
    return [text[:pos].strip()] + wrap_lines(text[pos:].strip(), max_width, max_lines - 1)


def build_cues(
    tokens: Sequence[Token],
    *,
    max_line_width: int = 42,
    max_lines: int = 2,
    max_dur: float = 7.0,
    min_dur: float = 1.0,
    max_gap: float = 1.2,
    min_chars: int = 1,
) -> List[Cue]:
    """核心函数：token 流 -> 字幕 cue 列表。"""
    total_limit = max_line_width * max_lines
    cues: List[Cue] = []
    cur: List[Token] = []
    cur_w = 0

    def flush() -> None:
        nonlocal cur, cur_w
        if not cur:
            return
        text = smart_join(cur)
        if text_width(text) < min_chars:
            # 太短（多为噪声或纯标点），并到上一条
            if cues:
                cues[-1].end = max(cues[-1].end, cur[-1].end)
                cues[-1].tokens.extend(cur)
                cues[-1].lines = wrap_lines(
                    smart_join(cues[-1].tokens), max_line_width, max_lines
                )
            cur, cur_w = [], 0
            return
        cues.append(
            Cue(
                start=cur[0].start,
                end=cur[-1].end,
                lines=wrap_lines(text, max_line_width, max_lines),
                tokens=list(cur),
            )
        )
        cur, cur_w = [], 0

    for tok in tokens:
        s = (tok.text or "").strip()
        if not s:
            continue

        # 静音超阈值：若当前 cue 已成型就断
        if cur and (tok.start - cur[-1].end) > max_gap:
            if (cur[-1].end - cur[0].start) >= min_dur:
                flush()

        w = text_width(s) + (1 if _needs_space(smart_join(cur), s) else 0)

        # 超长 / 超时：先断
        if cur and (cur_w + w > total_limit or (tok.end - cur[0].start) > max_dur):
            flush()

        cur.append(tok)
        cur_w += w

        # 句末标点：立即断
        if s[-1] in SENT_END:
            flush()
        # 句中标点 + 已经写满一行：断
        elif s[-1] in CLAUSE_END and cur_w >= max_line_width:
            flush()

    flush()
    return _post_process(cues, min_dur=min_dur, max_dur=max_dur)


def _post_process(cues: List[Cue], *, min_dur: float, max_dur: float) -> List[Cue]:
    """修掉过短、重叠、空行。"""
    clean = [c for c in cues if c.lines and any(l.strip() for l in c.lines)]
    clean.sort(key=lambda c: c.start)

    for i, c in enumerate(clean):
        # 不能压到下一句头上
        limit = clean[i + 1].start if i + 1 < len(clean) else float("inf")

        # 补足最小显示时长
        if c.duration < min_dur:
            target = min(c.start + min_dur, limit)
            c.end = max(c.end, target)

        # 略微延长到下一句出现前，减少闪烁感（最多延长 0.4s）
        if i + 1 < len(clean):
            gap = clean[i + 1].start - c.end
            if 0 < gap <= 0.4 and (c.end - c.start + gap) <= max_dur:
                c.end = clean[i + 1].start - 0.01

        # 兜底：不允许 end > limit
        if c.end > limit:
            c.end = max(c.start + 0.2, limit - 0.01)

    return clean
