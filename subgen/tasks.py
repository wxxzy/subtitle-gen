"""任务编排层 —— CLI 与 GUI 共用的处理入口。

设计意图：早期 `process_one()` 把「参数解析、日志打印、进度显示」和
「实际处理」揉在一起，导致 GUI 无法复用。这里把实际处理抽出来，
对外只依赖一个 RunOptions（纯数据）和一个 Reporter（输出通道）。

    CLI  -> build_options(argparse.Namespace) -> run_one(...)
    GUI  -> build_options(dict from JS)       -> run_one(...)

两条路径走的是同一份逻辑，行为天然一致。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .audio import FFmpeg, is_media_file, is_video
from .engine import TranscribeOptions, Transcriber
from .formats import write_subtitle
from .reporter import Cancelled, NullReporter, Reporter
from .segmenter import build_cues
from .translator import (
    TranslateOptions, lang_label, make_translator, norm_lang, translate_cues,
)


# 中文识别时喂给模型的简体引导语。
# Whisper 对中文常输出繁体（训练数据里繁体占比不低），加这句能明显减少繁体。
CN_PROMPT_HINT = "以下是简体中文普通话内容。"

# 中文语言代码（含粤语、繁体写法）
ZH_CODES = {"zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "chs", "cht", "cn", "yue"}


def _is_chinese(lang: Optional[str]) -> bool:
    """判断目标语言是否是中文（用于决定要不要做简繁转换、加简体引导）。"""
    if not lang:
        return False
    return lang.strip().lower().replace("_", "-") in ZH_CODES


def _glossary_terms(glossary: str) -> List[str]:
    """从术语表里取出"原词"部分，用于构造识别提示词。

    术语表格式是 `原词=译词`（每行一条）。识别阶段只需要原词 ——
    把这些词喂给模型，它就知道"妙脆角"是个真词，不会听成"妙翠脚"。
    """
    out: List[str] = []
    for line in (glossary or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=>", "=", "："):
            if sep in line:
                line = line.split(sep, 1)[0].strip()
                break
        if line and line not in out:
            out.append(line)
    return out


def _simplify(text: str) -> str:
    """繁体转简体。zhconv 缺失时原样返回（不因缺依赖而失败）。"""
    try:
        from zhconv import convert
    except ImportError:
        return text
    try:
        return convert(text, "zh-cn")
    except Exception:
        return text


def _apply_fixups(text: str, fixups: str) -> str:
    """按「错误词=正确词」替换表纠正识别结果。

    为什么要这个：同音字/方言词错误（如"妙翠脚"→"妙脆角"）光靠模型
    不一定能纠对，但人工一看就知道。配一张替换表，
    对**已生成的字幕**也能直接后处理，不用重新跑一遍 ASR。
    """
    if not text or not fixups:
        return text
    for line in fixups.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=>", "=", "："):
            if sep in line:
                wrong, right = line.split(sep, 1)
                wrong, right = wrong.strip(), right.strip()
                if wrong and right and wrong != right:
                    text = text.replace(wrong, right)
                break
    return text


def build_asr_prompt(
    *,
    prompt: Optional[str] = None,
    glossary: str = "",
    language: Optional[str] = None,
) -> Optional[str]:
    """组装喂给 ASR 的提示词（CLI 与 GUI 共用）。

    三部分：
      1. 用户手填的提示词（人名、专业术语）
      2. **术语表里的原词** —— 早期只把术语表喂给翻译，等于"识别错了再靠
         翻译纠正"，但错别字会污染译文。在识别源头就告诉模型正确写法才对。
      3. 简体引导（中文时），能明显减少繁体输出。

    实测（方言短剧）：加提示词后 large 模型的术语错误从 6 处降到 0 处。
    """
    parts: List[str] = []
    if prompt and prompt.strip():
        parts.append(prompt.strip())
    terms = _glossary_terms(glossary)
    if terms:
        parts.append("、".join(terms))
    # 只在**明确**是中文时加简体引导。
    # language 为空（自动检测）时不加 —— 否则英文素材会被塞进中文提示词，
    # 反而可能把模型带偏。（这是写测试时发现的边界问题）
    if _is_chinese(language):
        parts.append(CN_PROMPT_HINT)
    return "。".join(p for p in parts if p) or None


def load_rules(spec: Optional[str]) -> str:
    """读取纠错规则：可以是文件路径，也可以是内联规则字符串。"""
    if not spec:
        return ""
    s = spec.strip()
    if os.path.isfile(s):
        try:
            with open(s, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    return s


@dataclass
class RunOptions:
    """一次处理任务的全部参数。纯数据，不依赖 argparse 也不依赖 GUI。"""

    # 输入输出
    inputs: List[str] = field(default_factory=list)
    output: str = ""                    # 输出目录或文件名，空=与输入同目录
    formats: List[str] = field(default_factory=lambda: ["srt"])

    # ASR
    model: str = "turbo"
    device: str = "auto"
    compute_type: str = "auto"
    language: Optional[str] = None
    task: str = "transcribe"
    beam_size: int = 5
    prompt: Optional[str] = None
    vad: bool = True
    denoise: bool = False
    start: Optional[float] = None
    end: Optional[float] = None
    model_dir: str = ""
    # 识别结果后处理
    simplify: bool = True          # 繁体自动转简体（中文时生效）
    fixups: str = ""               # 纠错替换表，每行 "错误词=正确词"

    # 排版
    max_chars: int = 21
    max_lines: int = 2
    max_duration: float = 7.0
    min_duration: float = 1.0

    # 翻译
    translate: Optional[str] = None     # 目标语言代码
    src_lang: Optional[str] = None
    bilingual: bool = False
    only_translated: bool = False
    bilingual_only: bool = False
    mt_backend: str = "auto"
    mt_glossary: str = ""
    mt_context: str = ""
    mt_batch: int = 32
    mt_model_dir: str = ""
    api_key: str = ""
    api_base: str = ""
    llm_model: str = "deepseek-chat"

    # 去除原字幕（硬字幕遮盖）
    desub: bool = False
    desub_region: str = ""        # x,y,w,h；空则自动检测
    desub_method: str = "blur"
    desub_only: bool = False      # 只出干净视频，不生成字幕

    # 视频输出
    burn: bool = False
    mux: bool = False
    font: str = "Microsoft YaHei"
    font_size: int = 0

    # 其它
    json_out: Optional[str] = None


@dataclass
class FileResult:
    input: str
    ok: bool = False
    error: str = ""
    duration: float = 0.0
    cues: int = 0
    language: str = ""
    elapsed: float = 0.0
    speed: float = 0.0
    device: str = ""
    outputs: List[str] = field(default_factory=list)
    burned: str = ""
    muxed: str = ""
    cleaned: str = ""
    subtitle_preview: List[dict] = field(default_factory=list)


# ------------------------------------------------------------------ 工具

def line_width_for(max_chars: int, w: int, h: int) -> int:
    """按画面实际宽度决定每行能放多少半角字符。

    max_chars 是「汉字数」，竖屏视频屏宽有限，直接沿用会溢出。
    取用户设定与画面物理容量的较小值——画面优先。
    """
    user_w = max(6, max_chars) * 2
    if w <= 0:
        return user_w
    usable = w * 0.84
    fs = min(h * 0.045, usable / 20) if h > w else h * 0.05
    fs = max(14, fs)
    by_screen = int(usable / (fs * 0.5))
    return max(12, min(user_w, by_screen))


def outdir_of(path: str, src: str) -> str:
    """输出目录：空则用源文件目录；是目录就用它；是文件名就用其所在目录。"""
    if not path:
        return os.path.dirname(os.path.abspath(src))
    if os.path.splitext(path)[1]:
        return os.path.dirname(path) or "."
    return path


# ------------------------------------------------------------------ 单文件

def run_one(
    src: str,
    opts: RunOptions,
    *,
    reporter: Optional[Reporter] = None,
    transcriber: Optional[Transcriber] = None,
    translator: Optional[object] = None,
    model_path: Optional[str] = None,
) -> FileResult:
    """处理单个文件。CLI 和 GUI 都走这里。"""
    rep = reporter or NullReporter()
    ff = FFmpeg()
    res = FileResult(input=os.path.abspath(src))

    rep.check_cancel()

    if not os.path.isfile(src):
        res.error = "文件不存在"
        return res
    if not is_media_file(src):
        res.error = f"不支持的扩展名：{os.path.splitext(src)[1]}"
        return res
    if not ff.has_audio(src):
        res.error = "该文件没有音轨"
        return res

    dur = ff.duration(src)
    res.duration = round(dur, 2)

    clip = None
    if opts.start is not None or opts.end is not None:
        s = opts.start or 0.0
        e = opts.end if opts.end is not None else dur
        clip = (s, max(0.0, e - s))
        dur = clip[1]
        res.duration = round(dur, 2)

    tmp = None
    try:
        # 0) 去除原视频的硬字幕（可选）
        # 放在最前面：后续识别和导出都基于处理后的干净视频，
        # 这样烧录的新字幕不会叠在旧字幕上。
        work_src = src
        if opts.desub:
            from .desub import Region, detect_subtitle_region, default_output, remove_subtitles

            rep.stage("desub", "去除原字幕（遮盖）…")
            rep.check_cancel()

            regions: List[Region] = []
            if opts.desub_region:
                nums = [v.strip() for v in opts.desub_region.replace("，", ",").split(",")]
                if len(nums) == 4:
                    try:
                        regions = [Region(*(int(float(v)) for v in nums))]
                    except Exception:
                        regions = []
            if not regions:
                dr = detect_subtitle_region(src)
                if dr and dr.region:
                    regions = [dr.region]
                    rep.log(f"自动检测到字幕区 y={dr.region.y}（置信度 {dr.confidence:.2f}）")
                else:
                    rep.log(f"未检测到字幕，跳过去字幕：{dr.reason if dr else '未知'}")

            if regions:
                target = default_output(src, suffix=".clean")
                try:
                    remove_subtitles(src, target, regions, method=opts.desub_method)
                except Exception as e:
                    rep.log(f"去字幕失败（继续处理原视频）：{e}")
                else:
                    work_src = target
                    res.cleaned = os.path.abspath(target)
                    res.outputs.append(res.cleaned)
                    rep.log(f"已输出干净视频：{os.path.basename(target)}")

        # 只出干净视频
        if opts.desub_only and res.cleaned:
            res.ok = True
            rep.stage("done", "完成（仅输出去字幕视频）")
            return res

        # 1) 抽音轨
        rep.stage("audio", "抽取音轨（16 kHz 单声道）…", total=1.0)
        tmp = ff.extract_audio(
            src, sample_rate=16000, mono=True, denoise=opts.denoise,
            start=clip[0] if clip else None, duration=clip[1] if clip else None,
        )
        rep.update(1.0)
        rep.check_cancel()

        # 2) 识别
        rep.stage("asr", "语音识别中…", total=dur if dur > 0 else 1.0)

        # 组装识别提示词。术语表也参与识别、中文自动加简体引导，
        # 实测对专有名词和同音字改善明显（详见 build_asr_prompt 的说明）。
        # 注意：语言未指定时这里不加简体引导（怕带偏英文素材），
        # 但识别出结果后若确认是中文，会在下面补做繁简转换兜底。
        asr_prompt = build_asr_prompt(
            prompt=opts.prompt,
            glossary=opts.mt_glossary,
            language=opts.language,
        )

        # 别名（turbo/large…）必须解析成真实仓库名或本地目录，
        # 否则 faster-whisper 会当成 HF 仓库去联网找（踩过：触发了错误下载）
        from .models import resolve_model as _resolve
        topts = TranscribeOptions(
            model=_resolve(opts.model), device=opts.device, compute_type=opts.compute_type,
            language=opts.language, task=opts.task, beam_size=opts.beam_size,
            vad=opts.vad, initial_prompt=asr_prompt,
        )
        tr = transcriber or Transcriber(topts, verbose=False, model_path=model_path)
        # model_path 为空且模型不在项目缓存里时，主动确保下载到**项目目录**。
        # 否则 faster-whisper 会用自己的默认缓存（~/.cache/huggingface），
        # 出现"模型下到了别处、体积统计不到"的困惑（踩过）。
        if transcriber is None and not model_path and not os.path.isdir(topts.model):
            try:
                from .models import ensure_model as _ensure
                cache = opts.model_dir or os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
                p = _ensure(topts.model, cache_dir=cache, verbose=False)
                tr = Transcriber(topts, verbose=False, model_path=p)
            except Exception as e:
                rep.log(f"模型准备失败：{e}")
        if transcriber is not None:
            tr.opts = topts

        # 显式先加载模型，并把耗时如实告知。
        # 关键：load() 要把权重读进显存，large 模型在机械盘上要几十秒到几分钟。
        # 早期版本 load 发生在 transcribe 内部，那段时间界面毫无输出，
        # 用户完全不知道是在干活还是卡死了 —— 必须显式加载并上报。
        if not getattr(tr, "_model", None):
            rep.stage("load", "正在加载识别模型（首次较慢，请稍候）…", total=1.0)
            t_load = time.time()
            rep.check_cancel()
            tr.load()
            rep.update(1.0, extra=f"耗时 {time.time() - t_load:.1f}s")
            if getattr(tr, "verbose", False):
                pass
        rep.stage("asr", "语音识别中…", total=dur if dur > 0 else 1.0)

        def on_prog(done: float, total: float) -> None:
            rep.check_cancel()
            rep.update(done)

        t_res = tr.transcribe(tmp, on_progress=on_prog, total_duration=dur)
        res.language = t_res.language
        res.elapsed = round(t_res.elapsed, 2)
        res.speed = round(t_res.speed, 2)
        res.device = t_res.device
        rep.update(dur, extra=f"{t_res.speed:.1f}x 实时")

        # 2.5) 识别结果后处理
        # 分两层做，各有原因：
        #   - 简繁转换：逐字映射，可以安全地在 **token 层**做，时间轴不受影响
        #   - 纠错替换：需要整句上下文（错词可能被切成多个 token），
        #     所以放到断句之后的 **cue 层**做
        # 用**实际识别出的语言**判断，不能用默认值兜底 ——
        # 否则英文素材也会被"繁转简"，虽然对英文无害但没意义且浪费。
        zh = _is_chinese(res.language) or _is_chinese(opts.language) or _is_chinese(opts.src_lang)
        if opts.simplify and zh:
            for tok in t_res.tokens:
                if tok.text:
                    tok.text = _simplify(tok.text)

        # 3) 断句
        frame_w, frame_h = ff.video_size(work_src) if is_video(work_src) else (1920, 1080)
        line_w = line_width_for(opts.max_chars, frame_w, frame_h)

        cues = build_cues(
            t_res.tokens, max_line_width=line_w, max_lines=opts.max_lines,
            max_dur=opts.max_duration, min_dur=opts.min_duration,
        )
        if not cues:
            res.error = "未识别到任何语音内容（可能是纯音乐或静音）"
            return res
        res.cues = len(cues)

        # 纠错替换：在 cue 层做，因为错词可能被切成多个 token，
        # 只有拼成整句才能匹配上（如"妙 翠 脚"→"妙脆脚"）
        if opts.fixups:
            for c in cues:
                c.lines = [_apply_fixups(l, opts.fixups) for l in c.lines]

        if clip:
            for c in cues:
                c.start += clip[0]
                c.end += clip[0]

        # 4) 翻译
        src_lang_code = ""
        variants: List[tuple] = [("", cues, "")]
        if translator is not None and opts.translate:
            rep.check_cancel()
            tgt = norm_lang(opts.translate) or "en"
            src_lang = norm_lang(opts.src_lang or t_res.language) or "zh"
            src_lang_code = src_lang
            rep.stage("translate", f"翻译 {lang_label(src_lang)} → {lang_label(tgt)}…")

            try:
                tr_res = translate_cues(
                    cues, translator, target_lang=tgt,
                    max_line_width=line_w, max_lines=opts.max_lines,
                )
            except Cancelled:
                raise
            except Exception as e:
                res.error = f"翻译失败：{type(e).__name__}: {e}"
                # 翻译失败仍产出源语言字幕，不要整个任务白跑
                variants = [("", cues, "")]
            else:
                variants = []
                if not opts.bilingual_only:
                    if not opts.only_translated:
                        variants.append((f".{src_lang}", tr_res["src"], src_lang))
                    variants.append((f".{tgt}", tr_res["trans"], tgt))
                if opts.bilingual or opts.bilingual_only:
                    variants.append((f".{src_lang}-{tgt}.bilingual", tr_res["bilingual"], ""))
                if not variants:
                    variants = [("", cues, "")]

        # 5) 导出
        rep.stage("export", "导出字幕文件…")
        base, _ext = os.path.splitext(src)
        out_paths: List[str] = []
        mux_tracks: List[tuple] = []

        for suffix, vcues, vlang in variants:
            for fmt in opts.formats:
                if opts.output and len(opts.formats) == 1 and len(variants) == 1 \
                        and os.path.splitext(opts.output)[1]:
                    out = opts.output
                else:
                    d = outdir_of(opts.output, src)
                    if d and not os.path.isdir(d):
                        os.makedirs(d, exist_ok=True)
                    out = os.path.join(d, f"{os.path.basename(base)}{suffix}.{fmt}")

                kw = {}
                if fmt == "ass":
                    kw = dict(width=frame_w, height=frame_h,
                              font=opts.font, fontsize=opts.font_size)
                write_subtitle(out, vcues, fmt, **kw)
                out_paths.append(os.path.abspath(out))
                if vlang and fmt == "srt":
                    mux_tracks.append((os.path.abspath(out), vlang))

        res.outputs = out_paths

        # 字幕预览（GUI 用；CLI 忽略）
        preview_src = variants[0][1] if variants else cues
        for i, c in enumerate(preview_src[:200], 1):
            res.subtitle_preview.append({
                "index": i,
                "start": round(c.start, 3),
                "end": round(c.end, 3),
                "text": " ".join(c.lines),
                "trans": " ".join(c.trans_lines) if c.trans_lines else "",
            })

        vid_out_dir = outdir_of(opts.output, src) or os.path.dirname(os.path.abspath(src))
        if vid_out_dir and not os.path.isdir(vid_out_dir):
            os.makedirs(vid_out_dir, exist_ok=True)
        vid_base = os.path.join(vid_out_dir, os.path.basename(os.path.splitext(base)[0]))

        # 6) 烧录
        if opts.burn and is_video(src):
            rep.check_cancel()
            rep.stage("burn", "烧录硬字幕…")
            target = vid_base + ".hardburn.mp4"
            burn_srt = ""
            for suffix, _vc, _vl in variants:
                if suffix.endswith(".bilingual"):
                    cand = os.path.join(vid_out_dir, f"{os.path.basename(base)}{suffix}.srt")
                    if os.path.isfile(cand):
                        burn_srt = cand
                        break
            if not burn_srt:
                burn_srt = next((p for p in out_paths if p.endswith(".srt")), "")
            if not burn_srt:
                burn_srt = os.path.join(tempfile.gettempdir(), "subgen_burn.srt")
                write_subtitle(burn_srt, variants[-1][1], "srt")
            ff.burn(work_src, burn_srt, target, font=opts.font)
            res.burned = os.path.abspath(target)
            res.outputs.append(res.burned)

        # 7) 多轨封装
        if opts.mux and is_video(src):
            rep.check_cancel()
            rep.stage("mux", "封装多轨软字幕…")
            target = vid_base + ".softsub.mkv"
            tracks = list(mux_tracks)
            if not tracks:
                srt = next((p for p in out_paths if p.endswith(".srt")), "")
                if not srt:
                    srt = os.path.join(tempfile.gettempdir(), "subgen_mux.srt")
                    write_subtitle(srt, cues, "srt")
                tracks = [(srt, src_lang_code or "und")]
            if len(tracks) > 1:
                ff.mux_multi(work_src, tracks, target, default_lang=tracks[0][1])
            else:
                ff.mux_soft(work_src, tracks[0][0], target, lang=tracks[0][1])
            res.muxed = os.path.abspath(target)
            res.outputs.append(res.muxed)

        # 8) JSON（可选）
        if opts.json_out:
            import json as _json
            with open(opts.json_out, "w", encoding="utf-8") as f:
                _json.dump({
                    "input": res.input, "language": t_res.language,
                    "duration": dur, "model": opts.model, "device": t_res.device,
                    "compute_type": t_res.compute_type, "elapsed": t_res.elapsed,
                    "speed": t_res.speed,
                    "subtitles": res.subtitle_preview,
                }, f, ensure_ascii=False, indent=2)

        res.ok = True
        return res

    except Cancelled:
        res.error = "已取消"
        raise
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
