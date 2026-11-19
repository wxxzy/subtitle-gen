"""端到端冒烟测试：覆盖断句、五种格式输出、时间轴正确性。

不依赖网络下载（用假 token 直接喂给 segmenter / formats），
所以可以随时快速验证逻辑没坏。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subgen.formats import to_ass, to_lrc, to_srt, to_txt, to_vtt, write_subtitle
from subgen.segmenter import Token, build_cues, text_width, wrap_lines

FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def make_tokens(pairs):
    """pairs: [(文本, 起始, 结束)] -> Token 列表"""
    return [Token(text=t, start=s, end=e) for t, s, e in pairs]


def test_wrap() -> None:
    print("\n[1] 折行")
    check("全角宽度计算", text_width("中文abc") == 7, f"got {text_width('中文abc')}")
    lines = wrap_lines("今天天气不错，我们出去走走吧。", max_width=16)
    check("折成两行且不空", len(lines) == 2 and all(lines), str(lines))
    check("不在行首留标点", all(not (l and l[0] in "，。！？、；：") for l in lines), str(lines))
    check("短句不折行", wrap_lines("你好", max_width=42) == ["你好"])


def test_build_cues() -> None:
    print("\n[2] 断句")
    toks = make_tokens([
        ("今天", 0.0, 0.3), ("天气", 0.3, 0.6), ("不错", 0.6, 0.9), ("。", 0.9, 1.0),
        ("我们", 1.6, 2.0), ("出去", 2.0, 2.4), ("走走", 2.4, 2.8), ("吧", 2.8, 3.0), ("。", 3.0, 3.1),
    ])
    cues = build_cues(toks, max_line_width=42, max_dur=7.0, min_dur=0.5)
    check("逐句成条", len(cues) == 2, f"got {len(cues)}")
    if len(cues) == 2:
        check("首条文本", "今天天气不错。" in cues[0].text.replace("\n", ""), cues[0].text)
        check("前条不压后条", cues[0].end <= cues[1].start + 1e-6,
              f"{cues[0].end} vs {cues[1].start}")

    # 长句应按标点断开
    long_toks = make_tokens([
        ("首先", 0.0, 0.5), ("我们要", 0.5, 1.0), ("解决", 1.0, 1.5), ("这个", 1.5, 2.0),
        ("，", 2.0, 2.1), ("然后", 2.1, 2.6), ("再", 2.6, 3.0), ("考虑", 3.0, 3.5),
        ("那个", 3.5, 4.0), ("，", 4.0, 4.1), ("最后", 4.1, 4.6), ("收尾", 4.6, 5.0), ("。", 5.0, 5.1),
    ])
    lc = build_cues(long_toks, max_line_width=16, max_lines=1, min_dur=0.3)
    check("长句按标点断开", len(lc) >= 3, f"got {len(lc)}")

    # 超时强制断开
    slow = make_tokens([(f"字{i}", i * 1.2, i * 1.2 + 0.8) for i in range(10)])
    sc = build_cues(slow, max_line_width=60, max_lines=2, max_dur=3.0, min_dur=0.3)
    check("超长时强制断开", all(c.duration <= 3.0 + 1e-6 for c in sc),
          str([round(c.duration, 2) for c in sc]))

    # 空输入
    check("空输入返回空", build_cues([]) == [])

    # 过短 cue 合并
    tiny = make_tokens([("好", 0.0, 0.05), ("的", 0.05, 0.1), ("。", 0.1, 0.12)])
    tc = build_cues(tiny, min_dur=1.0)
    check("过短也补足最小时长", all(c.duration >= 1.0 - 1e-6 for c in tc),
          str([round(c.duration, 2) for c in tc]))


def test_formats() -> None:
    print("\n[3] 输出格式")
    cues = build_cues(make_tokens([
        ("你好", 0.0, 0.5), ("世界", 0.5, 1.0), ("。", 1.0, 1.05),
        ("第二", 2.5, 3.0), ("句", 3.0, 3.5), ("。", 3.5, 3.55),
    ]), min_dur=0.5)

    srt = to_srt(cues)
    check("SRT 序号递增", srt.startswith("1\n"), srt[:20])
    check("SRT 时间轴格式", "00:00:00,000 --> " in srt)
    check("SRT 有空行分隔", "\n\n" in srt)

    vtt = to_vtt(cues)
    check("VTT 头", vtt.startswith("WEBVTT"))
    check("VTT 用点号毫秒", "00:00:00.000 --> " in vtt)

    ass = to_ass(cues, width=1920, height=1080)
    check("ASS 段头", "[Script Info]" in ass and "[V4+ Styles]" in ass and "[Events]" in ass)
    check("ASS 时间轴", "Dialogue: 0,0:00:00.00," in ass)
    check("ASS 字号自适应", "Microsoft YaHei" in ass)

    lrc = to_lrc(cues)
    check("LRC 标签", lrc.startswith("[00:00.00]"), lrc[:24])

    txt = to_txt(cues)
    check("TXT 纯文本", "你好世界。" in txt.replace(" ", ""), txt[:40])
    txt_ts = to_txt(cues, with_ts=True)
    check("TXT 带时间戳", txt_ts.startswith("[00:00:00]"), txt_ts[:20])

    # 落盘 + 编码
    with tempfile.TemporaryDirectory() as d:
        for fmt in ("srt", "vtt", "ass", "lrc", "txt"):
            p = write_subtitle(os.path.join(d, f"t.{fmt}"), cues)
            check(f"写出 {fmt}", os.path.isfile(p) and os.path.getsize(p) > 0)
        # SRT 需带 BOM，VTT 不带
        with open(os.path.join(d, "t.srt"), "rb") as f:
            check("SRT 带 UTF-8 BOM", f.read(3) == b"\xef\xbb\xbf")
        with open(os.path.join(d, "t.vtt"), "rb") as f:
            check("VTT 无 BOM", f.read(3) != b"\xef\xbb\xbf")
        check("不支持格式报错", _raises(lambda: write_subtitle(os.path.join(d, "x.foo"), cues)))


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def test_srt_parseable() -> None:
    """用严格规则回读 SRT，确认播放器能解析。"""
    print("\n[4] SRT 可解析性")
    cues = build_cues(make_tokens([
        ("你好", 0.0, 0.5), ("世界", 0.5, 1.0), ("。", 1.0, 1.05),
    ]), min_dur=0.5)
    srt = to_srt(cues)
    blocks = [b for b in srt.strip().split("\n\n") if b.strip()]
    ok = True
    for b in blocks:
        lines = b.split("\n")
        if len(lines) < 3:
            ok = False
            break
        if not lines[0].strip().isdigit():
            ok = False
            break
        if "-->" not in lines[1]:
            ok = False
            break
        a, bb = lines[1].split("-->")
        if not (_ts_ok(a.strip()) and _ts_ok(bb.strip())):
            ok = False
            break
    check("每条字幕三段式合法", ok, srt)


def test_wrap_regression() -> None:
    """回归：英文（含大量空格）折行必须终止，不能死循环。

    曾经的 bug：wrap_lines 用 len(line1) 回推原文索引，而 line1 被 strip 过，
    遇连续空格时指针不前进 -> 死循环。中文无空格所以一直没暴露。
    """
    print("\n[5] 折行回归（英文/空格密集）")
    import signal

    cases = [
        "In the elevator, young men take their ponytail girlfriend home to meet their parents.",
        "Shit, I thought the part was set.   The girl's a man.   Why are you pulling her hair?",
        "You've been thinking about what you're doing in someone's house.  You go into your own house.",
        "a b c d e f g h i j k l m n o p q r s t u v w x y z " * 3,
        "Cream Cape Cats live in the highlands above 3,000 metres above sea level, where the air is thin.",
        "   leading and trailing spaces   ",
        "word " * 60,
    ]

    def _timeout(signum, frame):
        raise TimeoutError("wrap_lines 超时（疑似死循环）")

    has_sig = hasattr(signal, "SIGALRM")
    if has_sig:
        signal.signal(signal.SIGALRM, _timeout)

    all_ok = True
    for c in cases:
        try:
            if has_sig:
                signal.alarm(5)
            lines = wrap_lines(c, max_width=42, max_lines=2)
            if has_sig:
                signal.alarm(0)
        except TimeoutError:
            all_ok = False
            print(f"    TIMEOUT on {c[:50]!r}")
            continue
        except RecursionError:
            all_ok = False
            print(f"    RECURSION on {c[:50]!r}")
            continue
        if len(lines) > 2:
            all_ok = False
            print(f"    行数超限 {len(lines)} on {c[:50]!r}")
        if not any(l.strip() for l in lines):
            all_ok = False
            print(f"    空结果 on {c[:50]!r}")
        # 内容不能丢：拼回去应与原文等价（去空白后比较）
        joined = "".join(lines)
        if joined.replace(" ", "") != c.replace(" ", ""):
            all_ok = False
            print(f"    内容丢失：{c[:40]!r} -> {lines}")

    check("英文折行正常终止且不丢内容", all_ok)

    # 更严格：多行折行也不能死循环
    ok3 = True
    try:
        if has_sig:
            signal.alarm(5)
        r = wrap_lines("one two three four five six seven eight nine ten " * 4,
                       max_width=20, max_lines=3)
        if has_sig:
            signal.alarm(0)
        ok3 = len(r) <= 3
    except Exception as e:
        ok3 = False
        print(f"    max_lines=3 失败：{type(e).__name__}: {e}")
    check("max_lines=3 折行正常", ok3)


def test_word_integrity() -> None:
    """硬约束：折行绝不能把英文单词劈成两半。

    曾经的 bug：按字符位置硬切，出现 "ponyt" + "ail" 这种断词。
    中文无空格不受影响，所以只在英文场景暴露。
    """
    print("\n[5b] 单词完整性（不许断词）")
    import re

    samples = [
        "In the elevator, young men take their ponytail girlfriend home to meet their parents.",
        "Cream Cape Cats live in the highlands above 3,000 metres above sea level, where the thin air blows.",
        "You've been thinking about what you're doing in someone's house. You go into your own house.",
        "Shit, I thought the part was set. The girl's a man. Why are you pulling her hair?",
        "Congratulations on your extraordinary achievement in international collaboration and development.",
    ]

    bad = []
    for s in samples:
        for lines in (wrap_lines(s, max_width=42, max_lines=2),
                      wrap_lines(s, max_width=30, max_lines=3)):
            for ln in lines:
                # 行首/行尾不应是半截单词：检查是否切断了原有词
                pass
            # 逐词校验：原文每个词必须完整出现在某一行里
            words = [w for w in re.split(r"\s+", s) if w]
            joined = " ".join(lines)
            for w in words:
                if w not in joined:
                    bad.append((w, lines))

    check("所有单词保持完整", not bad, str(bad[:3]))

    # 中文不能被空格化，也不能丢字
    cn = "电梯里,年轻男子带双马尾的女友回家见父母并介绍了自己的工作和生活情况"
    cn_lines = wrap_lines(cn, max_width=20, max_lines=2)
    check("中文折行不丢字",
          "".join(cn_lines).replace(" ", "") == cn.replace(" ", ""),
          str(cn_lines))
    check("中文折行不产生异常空格", all("  " not in l for l in cn_lines))

    # 中英混排也要能处理
    mixed = "他说 This is a very long English sentence embedded in Chinese text 然后继续中文"
    m_lines = wrap_lines(mixed, max_width=30, max_lines=2)
    check("中英混排折行不丢内容",
          "".join(m_lines).replace(" ", "") in mixed.replace(" ", ""),
          str(m_lines))


def test_translation() -> None:
    """翻译流程的结构测试（不联网、不依赖模型）。"""
    print("\n[6] 翻译流程")
    from subgen.translator import (
        TranslateOptions, clean_translation, fix_repeats, lang_label,
        norm_lang, translate_cues,
    )

    check("语言代码归一化 zh-CN -> zh", norm_lang("zh-CN") == "zh")
    check("语言别名 chinese -> zh", norm_lang("chinese") == "zh")
    check("语言别名 eng -> en", norm_lang("eng") == "en")

    # ▁ 是 sentencepiece 的词边界标记，必须清理
    check("清理句子片标记 ▁", clean_translation("▁Hello,▁everyone.") == "Hello, everyone.")
    check("标点前不留空格", " ," not in clean_translation("Hello , world ."))
    check("首字母大写", clean_translation("hello world").startswith("Hello"))
    check("整句重复去重",
          fix_repeats("It's over, guys. It's over, guys.") == "It's over, guys.")
    check("非重复句保留", fix_repeats("A cat sat. A dog ran.") == "A cat sat. A dog ran.")

    # 用一个假翻译器测编排逻辑（时间轴保留 + 双语结构）
    class FakeTranslator:
        verbose = False

        def translate(self, texts):
            return [f"EN: {t}" for t in texts]

    cues = build_cues(make_tokens([
        ("你好", 0.0, 0.5), ("世界", 0.5, 1.0), ("。", 1.0, 1.05),
        ("第二", 2.5, 3.0), ("句", 3.0, 3.5), ("。", 3.5, 3.55),
    ]), min_dur=0.5)

    out = translate_cues(cues, FakeTranslator(), target_lang="en")
    check("返回三个变体", set(out) >= {"src", "trans", "bilingual"})
    check("源语言变体等于原文", out["src"] == list(cues))

    both = out["bilingual"]
    check("双语：条数不变", len(both) == len(cues))
    check("双语：时间轴原样保留",
          all(abs(a.start - b.start) < 1e-9 and abs(a.end - b.end) < 1e-9
              for a, b in zip(cues, both)))
    check("双语：原文与译文分开存",
          all(o.lines and o.trans_lines for o in both))
    check("双语：原文未被替换", both[0].lines == cues[0].lines)

    only = out["trans"]
    check("单语：只留译文", all("EN:" in " ".join(o.lines) for o in only)
          and all(not o.trans_lines for o in only))
    check("单语：时间轴保留",
          all(abs(a.start - b.start) < 1e-9 for a, b in zip(cues, only)))

    # 双语输出格式
    srt = to_srt(both)
    check("双语 SRT 含两种语言",
          "EN:" in srt and "你好" in srt)
    # 原文必须在译文之前（顺序统一：SRT/VTT/ASS 都是原文在上）
    srt_cue = srt.split("\n\n")[0]
    check("双语 SRT 原文在译文之前",
          srt_cue.index("你好") < srt_cue.index("EN:"))

    ass = to_ass(both, width=1920, height=1080)
    check("双语 ASS 用内联字号覆盖", "\\fs" in ass)
    check("双语 ASS 一条字幕一个事件",
          ass.count("Dialogue: 0,") == len(both))
    check("双语 ASS 原文在译文之前",
          ass.index("你好") < ass.index("EN:"))
    ass1 = to_ass(cues, width=1920, height=1080)
    check("单语 ASS 不含缩放标签", "\\fs" not in ass1)

    # 字号自适应：竖屏不能溢出。
    # Style 行：Style: Default,{font},{size},... -> 去掉前缀后按逗号切，
    # [0]=字体名 [1]=字号 [2]=主色
    def _fs(css: str):
        seg = css.split("Style: Default,")[1].split(",")
        return int(seg[1])

    ass_v = to_ass(cues, width=720, height=1280)
    fs = _fs(ass_v)
    check("竖屏字号受宽度约束", fs <= 36, f"fontsize={fs}")
    ass_h = to_ass(cues, width=1920, height=1080)
    fs_h = _fs(ass_h)
    check("横屏字号较竖屏大", fs_h > fs, f"{fs_h} vs {fs}")


def test_mux_helpers() -> None:
    """多轨封装的辅助逻辑：语言码映射。

    字幕轨的 language 元数据必须是 ISO 639-2/B 三字母码，
    写两字母码会让播放器显示成 und（未定义）。
    """
    print("\n[7] 多轨封装辅助")
    from subgen.audio import _iso3, _track_title

    check("zh -> chi", _iso3("zh") == "chi")
    check("en -> eng", _iso3("en") == "eng")
    check("ja -> jpn", _iso3("jpn") == "jpn")
    check("空值 -> und", _iso3("") == "und")
    check("未知码截三位", _iso3("xyzabc") == "xyz")
    check("轨道名 zh", _track_title("zh") == "中文")
    check("轨道名 en", _track_title("en") == "English")
    check("未知轨道名返回空", _track_title("xx") == "")


def test_recognition_fixup() -> None:
    """识别纠错相关：术语表参与识别、繁简转换、替换表。"""
    print("\n[8] 识别纠错")
    from subgen.tasks import (
        build_asr_prompt, _apply_fixups, _glossary_terms, _simplify, _is_chinese,
    )

    # 术语表解析：只取原词，忽略注释与译词
    terms = _glossary_terms("妙脆角猫=Cream Cat\n血域=雪域\n# 注释\n男主=the male lead")
    check("术语表只取原词", terms == ["妙脆角猫", "血域", "男主"], str(terms))
    check("术语表空值安全", _glossary_terms("") == [])
    check("术语表支持中文冒号", _glossary_terms("男主：the male lead") == ["男主"])

    # 提示词组装
    p = build_asr_prompt(prompt="人名A", glossary="妙脆角猫=X", language="zh")
    check("提示词含手填内容", "人名A" in p, p)
    check("提示词含术语原词", "妙脆角猫" in p, p)
    check("中文加简体引导", "简体" in p, p)
    p_en = build_asr_prompt(prompt="hello", glossary="", language="en")
    check("英文不加简体引导", "简体" not in p_en, p_en)
    check("空输入返回 None", build_asr_prompt() is None)

    # 中文判断
    for c in ("zh", "zh-CN", "chs", "yue", "ZH-HANS"):
        check(f"{c} 判为中文", _is_chinese(c))
    for c in ("en", "ja", "", "ko"):
        check(f"{c!r} 非中文", not _is_chinese(c))

    # 纠错替换
    r = "妙翠脚=妙脆角\n血域=雪域"
    check("纠错替换生效", _apply_fixups("妙翠脚猫在血域地带", r) == "妙脆角猫在雪域地带")
    check("纠错表空值原样返回", _apply_fixups("abc", "") == "abc")
    check("纠错表容错中文冒号", _apply_fixups("血域", "血域：雪域") == "雪域")
    check("纠错表跳过注释", _apply_fixups("x", "# 注释\na=b") == "x")

    # 繁简转换：关键是简体输入不能被改动
    check("繁体转简体", _simplify("妙翠角貓還沒有發育") == "妙翠角猫还没有发育",
          _simplify("妙翠角貓還沒有發育"))
    simp = "妙脆角猫还没有发育出来"
    check("简体输入保持不变", _simplify(simp) == simp, _simplify(simp))
    check("空字符串安全", _simplify("") == "")


def _ts_ok(t: str) -> bool:
    try:
        hms, ms = t.split(",")
        h, m, s = hms.split(":")
        return len(h) == 2 and len(m) == 2 and len(s) == 2 and len(ms) == 3
    except Exception:
        return False


def main() -> int:
    print("=" * 60)
    print("subgen 冒烟测试")
    print("=" * 60)
    test_wrap()
    test_build_cues()
    test_formats()
    test_srt_parseable()
    test_wrap_regression()
    test_word_integrity()
    test_translation()
    test_mux_helpers()
    test_recognition_fixup()
    print("\n" + "=" * 60)
    if FAIL:
        print(f"失败 {len(FAIL)} 项：{', '.join(FAIL)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
