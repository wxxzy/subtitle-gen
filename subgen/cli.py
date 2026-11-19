"""subgen 命令行入口。

最简用法：
    subgen video.mp4
批量：
    subgen *.mp4 -f srt vtt -o subs/
高级：
    subgen video.mp4 --model large --language zh --burn
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile
import time
from typing import List, Optional

from . import __version__
from .audio import FFmpeg, FFmpegNotFound, is_media_file, is_video
from .engine import TranscribeOptions, Transcriber
from .formats import write_subtitle
from .models import PRESETS, gpu_summary, resolve_model, ensure_model
from .segmenter import build_cues
from . import segmenter
from .tasks import build_asr_prompt, load_rules as _load_rules
from .translator import (
    TranslateOptions, lang_label, make_translator, norm_lang, translate_cues,
)

DEFAULT_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")


def _bar(done: float, total: float, width: int = 28) -> str:
    pct = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    n = int(pct * width)
    return f"[{'#' * n}{'.' * (width - n)}] {pct * 100:5.1f}%"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="subgen",
        description="视频/音频 -> 字幕 自动生成（本地运行，faster-whisper 驱动）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  subgen 视频.mp4                          # 生成同名 .srt
  subgen 视频.mp4 -f srt vtt ass           # 一次导出多种格式
  subgen 视频.mp4 --model large -l zh      # 指定大模型 + 中文
  subgen 视频.mp4 --burn -o 成品.mp4        # 直接烧录硬字幕
  subgen "./素材/*.mp4" -o "字幕/"          # 批量处理
  subgen --list-models                      # 查看可用模型
  subgen --check                            # 环境自检

字幕翻译：
  subgen 视频.mp4 -t en                    # 中文+英文各一份文件
  subgen 视频.mp4 -t en --bilingual        # 额外再加一份双语
  subgen 视频.mp4 -t en --only-translated  # 只要英文
  subgen 视频.mp4 -t en --mux              # 英文轨封进 MKV，播放器可切换
  subgen --list-langs                       # 查看翻译支持的语言
""",
    )
    p.add_argument("inputs", nargs="*", help="输入媒体文件，支持通配符")
    p.add_argument("-o", "--output", default="", help="输出文件或目录（默认：与输入同目录同名）")
    p.add_argument("-f", "--format", nargs="+", default=["srt"],
                   choices=["srt", "vtt", "ass", "lrc", "txt"], help="输出字幕格式，可多选")
    p.add_argument("-m", "--model", default="turbo",
                   help=f"模型：{'/'.join(PRESETS)} 或 HF 仓库名 / 本地目录（默认 turbo）")
    p.add_argument("-l", "--language", default=None,
                   help="语言代码，如 zh / en / ja；默认自动检测（中文场景建议显式指定 zh）")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    p.add_argument("--compute-type", default="auto",
                   choices=["auto", "float16", "float32", "int8", "int8_float16"])
    p.add_argument("--task", default="transcribe", choices=["transcribe", "translate"],
                   help="translate 会把语音直接翻成英文")
    p.add_argument("--beam-size", type=int, default=5, help="束搜索宽度，越大越准越慢（默认 5）")
    p.add_argument("--prompt", default=None, help="提示词：填人名/术语可显著改善专有名词识别")

    g = p.add_argument_group("字幕排版")
    g.add_argument("--max-chars", type=int, default=21, help="每行最多汉字数（默认 21）")
    g.add_argument("--max-lines", type=int, default=2, choices=[1, 2], help="每条字幕最多行数")
    g.add_argument("--max-duration", type=float, default=7.0, help="单条字幕最长秒数（默认 7）")
    g.add_argument("--min-duration", type=float, default=1.0, help="单条字幕最短秒数（默认 1）")
    g.add_argument("--no-simplify", action="store_true",
                   help="不把繁体识别结果转成简体（默认自动转简体）")
    g.add_argument("--fixups", "--fix", dest="fixups", default=None, metavar="FILE|RULES",
                   help="识别纠错表：文件路径或内联规则，每行 '错误词=正确词'。"
                        "例：--fixups \"妙翠脚=妙脆角，牛仔=男子\"")

    a = p.add_argument_group("音频处理")
    a.add_argument("--no-vad", action="store_true", help="关闭静音检测（默认开启，可减少幻听）")
    a.add_argument("--denoise", action="store_true", help="抽取音轨时做轻度降噪")
    a.add_argument("--start", type=float, default=None, help="只处理该时间点之后（秒）")
    a.add_argument("--end", type=float, default=None, help="只处理到该时间点（秒）")

    v = p.add_argument_group("视频输出")
    v.add_argument("--burn", action="store_true",
                   help="把字幕烧进视频画面（硬字幕，平台不支持多轨时用；有双语则烧双语）")
    v.add_argument("--mux", action="store_true",
                   help="把各语言字幕作为独立轨道封装进 MKV（画质无损，播放器可切换）")
    v.add_argument("--font", default="Microsoft YaHei", help="字幕字体")
    v.add_argument("--font-size", type=int, default=0, help="字号，0=按分辨率自动")
    v.add_argument("--desub", "--remove-subs", dest="desub", nargs="?", const="auto",
                   default=None, metavar="区域",
                   help="去除原视频的硬字幕（遮盖）。区域可为 auto（自动检测）、"
                        "bottom/bottom2/top 预设，或 x,y,w,h 像素值；多个用分号隔开")
    v.add_argument("--desub-top", action="store_true",
                   help="同时遮盖顶部标题区（默认只处理底部字幕）")
    v.add_argument("--desub-preview", action="store_true",
                   help="只检测并报告字幕位置，不实际处理")
    v.add_argument("--desub-only", action="store_true",
                   help="只输出去字幕的干净视频，不生成字幕（用于后续挂软字幕）")

    m = p.add_argument_group("模型管理")
    m.add_argument("--model-dir", default=DEFAULT_CACHE, help="模型缓存目录")
    m.add_argument("--no-mirror", action="store_true", help="不用国内镜像下载模型")
    m.add_argument("--refresh", action="store_true", help="强制重新下载模型")

    tr = p.add_argument_group(
        "字幕翻译",
        "把生成的字幕翻译成其他语言。默认走离线模型（无需联网、免费、极快）；"
        "若配置了 API Key 则自动改用 LLM（质量更好，尤其口语化内容）。",
    )
    tr.add_argument("--translate", "-t", metavar="LANG", default=None,
                    help="目标语言，如 en / 英文 / japanese；传 auto 则翻成英文（默认）")
    tr.add_argument("--src-lang", metavar="LANG", default=None,
                    help="源语言。默认取转录时识别出的语言")
    tr.add_argument("--bilingual", action="store_true",
                    help="额外输出双语字幕（原文+译文同屏）。不加则源语言与目标语言各出一份文件")
    tr.add_argument("--only-translated", action="store_true",
                    help="只输出译文，不生成源语言字幕文件（默认两者都出）")
    tr.add_argument("--bilingual-only", action="store_true",
                    help="只输出双语字幕，不单独生成单语文件")
    tr.add_argument("--mt-backend", default="auto", choices=["auto", "offline", "llm"],
                    help="翻译后端：auto 自动选，offline 离线模型，llm 大模型（默认 auto）")
    tr.add_argument("--mt-model-dir", default="", help="离线翻译模型缓存目录")
    tr.add_argument("--mt-glossary", default=None,
                    help="术语表文件，每行一条 '原词=译词'（如 妙脆角猫=Cream Cat）")
    tr.add_argument("--mt-context", default=None,
                    help="领域/风格提示，仅 LLM 后端生效（如 '游戏解说，口语化，可保留网络用语'）")
    tr.add_argument("--mt-batch", type=int, default=32, help="每批翻译条数（默认 32）")
    tr.add_argument("--api-key", default=None, help="LLM API Key（或设 SUBGEN_API_KEY）")
    tr.add_argument("--api-base", default=None, help="LLM API 地址（或设 SUBGEN_API_BASE）")
    tr.add_argument("--llm-model", default=os.environ.get("SUBGEN_LLM_MODEL", "deepseek-chat"),
                    help="LLM 模型名（默认 deepseek-chat）")

    p.add_argument("--list-models", action="store_true", help="列出内置模型")
    p.add_argument("--list-langs", action="store_true", help="列出翻译支持的语言")
    p.add_argument("--check", action="store_true", help="检查运行环境")
    p.add_argument("-j", "--jobs", type=int, default=0, help="并行处理文件数（0=自动）")
    p.add_argument("--json", dest="json_out", default=None, help="额外输出结构化结果 JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    p.add_argument("--version", action="version", version=f"subgen {__version__}")
    return p


# ------------------------------------------------------------------ 环境自检

def cmd_check() -> int:
    print("=" * 58)
    print(f"subgen {__version__}  环境自检")
    print("=" * 58)

    print("\n[1] ffmpeg")
    try:
        ff = FFmpeg()
        import subprocess

        v = subprocess.run([ff.ffmpeg, "-version"], capture_output=True, text=True)
        print(f"    OK  {v.stdout.splitlines()[0]}")
        print(f"    ffprobe: {'OK' if ff.ffprobe else '未找到（将用 ffmpeg 兜底解析）'}")
    except FFmpegNotFound as e:
        print(f"    缺失：{e}")
        return 1

    print("\n[2] Python 依赖")
    ok = True
    for mod, hint in (("faster_whisper", "faster-whisper"), ("ctranslate2", "faster-whisper"),
                      ("huggingface_hub", "huggingface-hub")):
        try:
            __import__(mod)
            print(f"    OK  {mod}")
        except ImportError:
            print(f"    缺失 {mod}  -> pip install {hint}")
            ok = False

    print("\n[3] 计算设备")
    info = gpu_summary()
    if info["count"] > 0:
        names = " / ".join(info["names"]) or "未知型号"
        cc = info["cc"]
        print(f"    CUDA 设备 {info['count']} 个：{names}")
        print(f"    计算能力 CC {cc if cc is not None else '未知'}")
        print(f"    建议 compute_type：{info['recommended_compute_type']}")
        if cc is not None and cc < 7.0:
            print("    提示：Pascal 架构 FP16 被限速，已选用 int8（实测比 float32 快 13~18 倍）")
    else:
        print("    未检测到 NVIDIA GPU，将使用 CPU（int8）")
        print("    提示：模型选 small 或 medium，速度更可接受")

    print("\n[4] 模型缓存")
    d = DEFAULT_CACHE
    if os.path.isdir(d):
        print(f"    {d}")
        found_any = False
        # 识别模型：目录名形如 <owner>--<repo>，且里面有真实的 model.bin
        for x in sorted(os.listdir(d)):
            sub = os.path.join(d, x)
            if not os.path.isdir(sub) or x in ("mt", ".locks"):
                continue
            mb = _find_model_bin(sub)
            if mb:
                found_any = True
                size_gb = os.path.getsize(mb) / 1024 ** 3
                print(f"      - {x.replace('--', '/')}  ({size_gb:.2f} GB)")
        # 翻译模型
        mt = os.path.join(d, "mt")
        if os.path.isdir(mt):
            for x in sorted(os.listdir(mt)):
                p = os.path.join(mt, x)
                if os.path.isdir(p):
                    found_any = True
                    print(f"      - 翻译模型 {x}")
        if not found_any:
            print("      （空，首次运行会自动下载）")
    else:
        print(f"    {d} 尚未创建，首次运行会自动创建并下载")

    print("\n" + "=" * 58)
    print("自检通过，可以直接使用。" if ok else "存在缺失项，请先按提示补齐。")
    return 0 if ok else 1


def _find_model_bin(root: str) -> str:
    """在目录里找一个真实的 model.bin（>1KB），返回路径或空串。

    不用固定层级：早期版本是 HF 的 snapshots/<hash>/model.bin，
    现在是平铺的 model.bin。两种都要能识别。
    """
    for cur, dirs, files in os.walk(root):
        if cur[len(root):].count(os.sep) > 3:
            dirs[:] = []
            continue
        if "model.bin" in files:
            p = os.path.join(cur, "model.bin")
            try:
                if os.path.getsize(p) > 1024:
                    return p
            except OSError:
                pass
    return ""


def cmd_list_models() -> int:
    print(f"{'名称':<10}{'显存需求':<10}说明")
    print("-" * 72)
    for k, p in PRESETS.items():
        print(f"{k:<10}{str(p.vram_gb) + ' GB':<10}{p.label}")
        print(f"{'':<20}{p.note}")
    print("\n也可以直接填 HF 仓库名或本地模型目录。")
    print(f"模型缓存位置：{DEFAULT_CACHE}")
    return 0


# 离线翻译可用的语言方向（来源：Argos 官方包索引，100 个方向）
# 这里的每条都是「直连」方向；非直连的可经英文中转两跳。
TRANSLATE_DIRECT = [
    ("zh → en", "中 → 英"), ("en → zh", "英 → 中"),
    ("ja → en", "日 → 英"), ("en → ja", "英 → 日"),
    ("ko → en", "韩 → 英"), ("en → ko", "英 → 韩"),
    ("fr → en", "法 → 英"), ("en → fr", "英 → 法"),
    ("de → en", "德 → 英"), ("en → de", "英 → 德"),
    ("es → en", "西 → 英"), ("en → es", "英 → 西"),
    ("ru → en", "俄 → 英"), ("en → ru", "英 → 俄"),
    ("pt → en", "葡 → 英"), ("en → pt", "英 → 葡"),
    ("it → en", "意 → 英"), ("en → it", "英 → 意"),
    ("ar → en", "阿 → 英"), ("en → ar", "英 → 阿"),
    ("th → en", "泰 → 英"), ("en → th", "英 → 泰"),
    ("vi → en", "越 → 英"), ("en → vi", "英 → 越"),
    ("id → en", "印尼 → 英"), ("en → id", "英 → 印尼"),
    ("hi → en", "印地 → 英"), ("en → hi", "英 → 印地"),
    ("nl → en", "荷 → 英"), ("en → nl", "英 → 荷"),
    ("pl → en", "波 → 英"), ("en → pl", "英 → 波"),
    ("tr → en", "土 → 英"), ("en → tr", "英 → 土"),
    ("uk → en", "乌 → 英"), ("en → uk", "英 → 乌"),
    ("he → en", "希伯来 → 英"), ("en → he", "英 → 希伯来"),
    ("ms → en", "马来 → 英"), ("en → ms", "英 → 马来"),
    ("sv → en", "瑞典 → 英"), ("en → sv", "英 → 瑞典"),
    ("da → en", "丹 → 英"), ("en → da", "英 → 丹"),
    ("fi → en", "芬 → 英"), ("en → fi", "英 → 芬"),
    ("cs → en", "捷 → 英"), ("en → cs", "英 → 捷"),
]


def cmd_list_langs() -> int:
    print("=" * 66)
    print("翻译支持的语言")
    print("=" * 66)
    print("\n【离线后端】无需联网、无需 API Key，模型自动下载（约 75 MB/方向）")
    print("  直连方向（速度最快、质量最好）：")
    half = (len(TRANSLATE_DIRECT) + 1) // 2
    for i in range(half):
        left = TRANSLATE_DIRECT[i]
        right = TRANSLATE_DIRECT[i + half] if i + half < len(TRANSLATE_DIRECT) else ("", "")
        print(f"    {left[0]:<12}{left[1]:<12}    {right[0]:<12}{right[1]}")
    print("\n  其他语言对：自动经英文中转（如 日→中 走 日→en→zh），质量略降")
    print("  注：Zh→X 这种写法会走 en 中转，因为官方包只有 X→en / en→X 两种方向")

    print("\n【LLM 后端】需 API Key，质量最好，尤其适合口语化/网络用语内容")
    print("  支持任意 OpenAI 兼容接口：DeepSeek / OpenAI / 通义 / Kimi / 火山方舟 等")
    print("  语言覆盖：模型支持多少就是多少，远超离线后端")

    print("\n" + "-" * 66)
    print("用法示例：")
    print("  subgen 视频.mp4 -t en                    # 中→英，只出英文字幕")
    print("  subgen 视频.mp4 -t en --bilingual        # 中英双语同屏")
    print("  subgen 视频.mp4 -t ja --mt-backend llm   # 用大模型翻成日文")
    return 0


# ------------------------------------------------------------------ 单文件处理

def _is_zh(lang: Optional[str]) -> bool:
    """是否中文（决定要不要做繁简转换）。"""
    if not lang:
        return False
    return lang.strip().lower().replace("_", "-") in {
        "zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "chs", "cht", "cn", "yue"
    }


def _parse_desub_regions(spec: str, src: str, include_top: bool = False) -> List:
    """解析 --desub 的区域参数，返回 Region 列表。

    支持：
      auto                自动检测
      bottom/bottom2/top  按预设（相对画面比例）
      x,y,w,h             具体像素（多个用分号隔开）
    """
    from .audio import FFmpeg
    from .desub import Region, detect_subtitle_region, preset_regions

    if not spec:
        return []

    ff = FFmpeg()
    w, h = (1920, 1080)
    if is_video(src):
        w, h = ff.video_size(src)

    out: List[Region] = []
    for part in str(spec).split(";"):
        p = part.strip().lower()
        if not p:
            continue
        if p in ("auto", "1", "true", "yes"):
            r = detect_subtitle_region(src)
            if r and r.region:
                out.append(r.region)
                print(f"      自动检测到字幕区 y={r.region.y}~{r.region.y+r.region.h}"
                      f"（置信度 {r.confidence:.2f}，{r.reason}）")
            else:
                print(f"      自动检测未找到字幕区：{r.reason if r else '未知'}")
        elif p in ("bottom", "bottom2", "bottom_low", "top"):
            out.extend(preset_regions(p, w, h))
        else:
            nums = [x.strip() for x in p.replace("，", ",").split(",")]
            if len(nums) == 4:
                try:
                    x, y, rw, rh = (int(float(v)) for v in nums)
                    out.append(Region(x=x, y=y, w=rw, h=rh))
                except ValueError:
                    print(f"      区域格式无法识别，已跳过：{part}")

    if include_top and not any(r.y < h * 0.25 for r in out):
        out.extend(preset_regions("top", w, h))

    return out


def _line_width_for(args, w: int, h: int) -> int:
    """按画面实际宽度决定每行能放多少半角字符。

    用户给的 --max-chars 是「汉字数」，竖屏视频屏宽有限，直接沿用会溢出。
    这里取用户设定与画面物理容量的较小值——画面优先，避免字幕出界。
    """
    user_w = max(6, args.max_chars) * 2      # 汉字 -> 半角
    if w <= 0:
        return user_w
    # 两侧各留 8% 边距，字号约为屏高的 5%（竖屏时还要受宽度约束，见 formats.to_ass）
    usable = w * 0.84
    fs = min(h * 0.045, usable / 20) if h > w else h * 0.05
    fs = max(14, fs)
    by_screen = int(usable / (fs * 0.5))     # 半角字符平均宽度约为字号的一半
    return max(12, min(user_w, by_screen))


def process_one(
    src: str,
    args,
    *,
    transcriber: Optional[Transcriber] = None,
    translator: Optional[object] = None,
    quiet: bool = False,
    model_path: Optional[str] = None,
) -> dict:
    ff = FFmpeg()
    result: dict = {"input": os.path.abspath(src), "ok": False}

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    if not os.path.isfile(src):
        result["error"] = "文件不存在"
        return result
    if not is_media_file(src):
        result["error"] = f"不支持的扩展名：{os.path.splitext(src)[1]}"
        return result
    if not ff.has_audio(src):
        result["error"] = "该文件没有音轨"
        return result

    dur = ff.duration(src)
    result["duration"] = round(dur, 2)

    log(f"\n{'=' * 58}")
    log(f"输入：{os.path.basename(src)}")
    if dur > 0:
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        log(f"时长：{h:d}:{m:02d}:{s:02d}")
    log(f"{'=' * 58}")

    # ── 去字幕预览模式：只报告检测结果，不做任何处理
    if getattr(args, "desub_preview", False):
        from .desub import detect_subtitle_region
        log("检测硬字幕位置（不改动文件）…")
        r = detect_subtitle_region(src)
        if r and r.region:
            g = r.region
            w0, h0 = ff.video_size(src) if is_video(src) else (1920, 1080)
            log(f"  检测区域: x={g.x} y={g.y} w={g.w} h={g.h}")
            log(f"  相对位置: y {g.y / h0 * 100:.1f}% ~ {(g.y + g.h) / h0 * 100:.1f}%，"
                f"高度占比 {g.h / h0 * 100:.1f}%")
            log(f"  置信度  : {r.confidence:.2f}  （{r.reason}）")
            if r.confidence < 0.6:
                log("  提示    : 置信度偏低，建议用 --desub 具体像素值手动指定")
            log(f"\n  确认后可用：--desub \"{g.x},{g.y},{g.w},{g.h}\"")
        else:
            log(f"  未检测到字幕：{r.reason if r else '未知'}")
            log("  可尝试 --desub bottom（按预设遮盖底部区域）")
        # 预览是只读操作，不算失败（早期返回空 result 会被统计成"失败 1"）
        result["ok"] = True
        result["preview_only"] = True
        return result

    # ── 去字幕：先产出干净视频，后续识别/导出都基于它
    work_src = src
    if getattr(args, "desub", None):
        from .desub import default_output, remove_subtitles
        if not is_video(src):
            log("      提示：去字幕只对视频有效，已跳过")
        else:
            log("[0/3] 去除原字幕（遮盖）…")
            regions = _parse_desub_regions(
                args.desub, src, include_top=bool(getattr(args, "desub_top", False))
            )
            if not regions:
                log("      未得到有效区域，跳过去字幕")
            else:
                t0 = time.time()
                for g in regions:
                    log(f"      遮盖区: x={g.x} y={g.y} w={g.w} h={g.h}")
                target = default_output(src, suffix=".clean")
                try:
                    remove_subtitles(src, target, regions)
                except Exception as e:
                    log(f"      去字幕失败：{e}")
                else:
                    work_src = target
                    result["cleaned"] = os.path.abspath(target)
                    log(f"      完成，用时 {time.time() - t0:.1f}s")
                    log(f"      -> {os.path.abspath(target)}")

    # 只出干净视频（不生成字幕）
    if getattr(args, "desub_only", False) and result.get("cleaned"):
        result["ok"] = True
        return result

    # 时间裁剪
    clip = None
    if args.start is not None or args.end is not None:
        s = args.start or 0.0
        e = args.end if args.end is not None else dur
        clip = (s, max(0.0, e - s))
        dur = clip[1]
        result["duration"] = round(dur, 2)

    tmp = None
    try:
        # 1) 抽音轨
        log("[1/3] 抽取音轨（16 kHz 单声道）…")
        t0 = time.time()
        tmp = ff.extract_audio(
            src,
            sample_rate=16000,
            mono=True,
            denoise=args.denoise,
            start=clip[0] if clip else None,
            duration=clip[1] if clip else None,
        )
        size_mb = os.path.getsize(tmp) / 1024 / 1024
        log(f"      完成，用时 {time.time() - t0:.1f}s，音频 {size_mb:.1f} MB")

        # 2) 转写
        log("[2/3] 识别中…")
        # 提示词组装复用 tasks 里的逻辑（术语表也参与识别 + 中文简体引导），
        # 避免 CLI 和 GUI 两套实现走偏
        opts = TranscribeOptions(
            model=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
            task=args.task,
            beam_size=args.beam_size,
            vad=not args.no_vad,
            initial_prompt=build_asr_prompt(
                prompt=args.prompt,
                glossary=getattr(args, "mt_glossary", "") or "",
                language=args.language,
            ),
        )
        tr = transcriber or Transcriber(opts, verbose=not quiet, model_path=model_path)
        if transcriber is not None:
            tr.opts = opts  # 复用连接，仅更新参数

        # 识别结果后处理用到的两个开关
        do_simplify = not getattr(args, "no_simplify", False)
        fixup_rules = _load_rules(getattr(args, "fixups", None))

        last = [0.0]

        def on_prog(done: float, total: float) -> None:
            if quiet:
                return
            if done - last[0] > max(2.0, total * 0.03) or done >= total:
                last[0] = done
                print(f"      {_bar(done, total)}  {done:.1f}/{total:.1f}s", end="\r", flush=True)

        res = tr.transcribe(tmp, on_progress=on_prog, total_duration=dur)
        if not quiet:
            print(" " * 70, end="\r")
        log(
            f"      完成，用时 {res.elapsed:.1f}s（{res.speed:.1f}x 实时）"
            f"  语言 {res.language}({res.language_probability:.0%})"
            f"  设备 {res.device}/{res.compute_type}"
        )
        result.update(
            language=res.language,
            language_probability=round(res.language_probability, 3),
            elapsed=round(res.elapsed, 2),
            speed=round(res.speed, 2),
            device=res.device,
            compute_type=res.compute_type,
            tokens=len(res.tokens),
        )

        # 识别结果后处理：逐字层面做繁简转换（不影响时间轴）
        if do_simplify and _is_zh(res.language or args.language):
            from .tasks import _simplify
            n_before = sum(1 for t in res.tokens if t.text)
            changed = 0
            for tok in res.tokens:
                if tok.text:
                    new = _simplify(tok.text)
                    if new != tok.text:
                        changed += 1
                    tok.text = new
            if changed and not quiet:
                log(f"      已将 {changed} 处繁体转为简体")

        # 画面尺寸：折行与 ASS 字号共用，保证不出界
        frame_w, frame_h = ff.video_size(src) if is_video(src) else (1920, 1080)
        line_w = _line_width_for(args, frame_w, frame_h)

        # 3) 断句 + 导出
        log("[3/3] 智能断句并导出…")
        cues = build_cues(
            res.tokens,
            max_line_width=line_w,
            max_lines=args.max_lines,
            max_dur=args.max_duration,
            min_dur=args.min_duration,
        )

        # 纠错替换：在成句层面做（错词可能被切成多个 token，拼起来才匹配得上）
        if fixup_rules and cues:
            from .tasks import _apply_fixups
            hits = 0
            for c in cues:
                new_lines = []
                for l in c.lines:
                    nl = _apply_fixups(l, fixup_rules)
                    if nl != l:
                        hits += 1
                    new_lines.append(nl)
                c.lines = new_lines
            if hits and not quiet:
                log(f"      纠错表命中 {hits} 处")

        if not cues:
            result["error"] = "未识别到任何语音内容（可能是纯音乐或静音）"
            log("      未识别到内容")
            return result

        # 时间偏移回原片
        if clip:
            for c in cues:
                c.start += clip[0]
                c.end += clip[0]

        result["cues"] = len(cues)
        log(f"      生成 {len(cues)} 条字幕")

        # 3.5) 翻译（可选）
        # 每个变体 = (文件名后缀, cues, 语言标签)。语言标签用于软字幕轨道的
        # language 元数据，播放器据此显示"中文/English"并可切换。
        src_lang_code = ""
        variants: List[tuple] = [("", cues, "")]   # 不翻译时只有一个原始变体
        if translator is not None and getattr(args, "translate", None):
            tgt = norm_lang(args.translate) or "en"
            src_lang = norm_lang(args.src_lang or res.language) or "zh"
            src_lang_code = src_lang
            log(f"[翻译] {lang_label(src_lang)} → {lang_label(tgt)}")

            try:
                res_tr = translate_cues(
                    cues,
                    translator,
                    target_lang=tgt,
                    max_line_width=line_w,
                    max_lines=args.max_lines,
                )
            except Exception as e:
                log(f"      翻译失败：{type(e).__name__}: {e}")
                result["translate_error"] = str(e)
            else:
                result["translated"] = tgt
                empty = sum(1 for c in res_tr["trans"] if not c.lines)
                if empty:
                    log(f"      注意：{empty} 条译文为空，已回退原文")

                # 按用户选择决定出哪些变体
                variants = []
                if not args.bilingual_only:
                    if not args.only_translated:
                        variants.append((f".{src_lang}", res_tr["src"], src_lang))
                    variants.append((f".{tgt}", res_tr["trans"], tgt))
                if args.bilingual or args.bilingual_only:
                    variants.append(
                        (f".{src_lang}-{tgt}.bilingual", res_tr["bilingual"], "")
                    )
                if not variants:
                    variants = [("", cues, "")]

        base, ext = os.path.splitext(src)

        def outdir_of(path: str) -> str:
            """输出目录：--output 是目录就用它，是文件名就用其所在目录。"""
            if not path:
                return os.path.dirname(os.path.abspath(src))
            if os.path.splitext(path)[1]:
                return os.path.dirname(path) or "."
            return path

        out_paths: List[str] = []
        # 收集各语言变体的字幕路径，供多轨封装使用
        mux_tracks: List[tuple] = []

        for suffix, vcues, vlang in variants:
            for fmt in args.format:
                if args.output and len(args.format) == 1 and len(variants) == 1 \
                        and os.path.splitext(args.output)[1]:
                    # 单变体 + 明确文件名：尊重用户指定
                    out = args.output
                else:
                    d = outdir_of(args.output)
                    if d and not os.path.isdir(d):
                        os.makedirs(d, exist_ok=True)
                    out = os.path.join(d, f"{os.path.basename(base)}{suffix}.{fmt}")

                kw = {}
                if fmt == "ass":
                    kw = dict(width=frame_w, height=frame_h,
                              font=args.font, fontsize=args.font_size)

                write_subtitle(out, vcues, fmt, **kw)
                out_paths.append(os.path.abspath(out))
                if vlang and fmt == "srt":
                    mux_tracks.append((os.path.abspath(out), vlang))
                log(f"      -> {os.path.abspath(out)}")

        result["outputs"] = out_paths

        # 成片（烧录/封装）也跟随 --output 目录，不要写回源视频所在目录——
        # 源目录常是下载器管的素材区，往里塞成片会把素材目录弄乱。
        vid_out_dir = outdir_of(args.output) or os.path.dirname(os.path.abspath(src))
        if vid_out_dir and not os.path.isdir(vid_out_dir):
            os.makedirs(vid_out_dir, exist_ok=True)
        vid_base = os.path.join(vid_out_dir, os.path.basename(os.path.splitext(base)[0]))

        # 可选：烧录 / 软字幕封装
        # 烧录必须挑「要显示的那一份」——双语场景下当然要烧双语的，
        # 否则只烧了译文，中文原文白生成了。
        if args.burn and is_video(src):
            log("[附加] 烧录硬字幕…")
            target = vid_base + ".hardburn.mp4"
            burn_srt = ""
            for suffix, _vc, _vl in variants:
                if suffix.endswith(".bilingual"):
                    cand = os.path.join(vid_out_dir,
                                        f"{os.path.basename(base)}{suffix}.srt")
                    if os.path.isfile(cand):
                        burn_srt = cand
                        break
            if not burn_srt:
                for p in out_paths:
                    if p.endswith(".srt"):
                        burn_srt = p
                        break
            if not burn_srt:
                burn_srt = os.path.join(tempfile.gettempdir(), "subgen_burn.srt")
                write_subtitle(burn_srt, variants[-1][1], "srt")
            if not quiet:
                print(f"      使用字幕：{os.path.basename(burn_srt)}")
            ff.burn(work_src, burn_srt, target, font=args.font)
            result["burned"] = os.path.abspath(target)
            log(f"      -> {os.path.abspath(target)}")

        if args.mux and is_video(src):
            log("[附加] 封装软字幕…")
            target = vid_base + ".softsub.mkv"
            tracks = list(mux_tracks)
            if not tracks:
                # 没翻译时也可以封装单一轨道
                srt = next((p for p in out_paths if p.endswith(".srt")), None)
                if not srt:
                    srt = os.path.join(tempfile.gettempdir(), "subgen_mux.srt")
                    write_subtitle(srt, cues, "srt")
                tracks = [(srt, src_lang_code or "und")]

            if len(tracks) > 1:
                ff.mux_multi(work_src, tracks, target, default_lang=tracks[0][1])
                if not quiet:
                    names = " / ".join(lang_label(l) for _, l in tracks)
                    print(f"      已封装 {len(tracks)} 条字幕轨：{names}")
            else:
                ff.mux_soft(work_src, tracks[0][0], target, lang=tracks[0][1])
            result["muxed"] = os.path.abspath(target)
            log(f"      -> {os.path.abspath(target)}")

        if args.json_out:
            import json as _json

            with open(args.json_out, "w", encoding="utf-8") as f:
                _json.dump(
                    {
                        "input": result["input"],
                        "language": res.language,
                        "duration": dur,
                        "model": args.model,
                        "device": res.device,
                        "compute_type": res.compute_type,
                        "elapsed": res.elapsed,
                        "speed": res.speed,
                        "subtitles": [
                            {"index": i, "start": round(c.start, 3), "end": round(c.end, 3),
                             "text": " ".join(c.lines)}
                            for i, c in enumerate(cues, 1)
                        ],
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            log(f"      -> {os.path.abspath(args.json_out)}")

        result["ok"] = True
        return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        if not quiet:
            print(f"      失败：{result['error']}")
        return result
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------ main

def expand_inputs(patterns: List[str]) -> List[str]:
    out: List[str] = []
    for pat in patterns:
        if os.path.isfile(pat):
            out.append(pat)
            continue
        hits = sorted(glob.glob(pat, recursive=True)) or sorted(glob.glob(os.path.join(pat, "*")))
        for h in hits:
            if os.path.isfile(h) and is_media_file(h):
                out.append(h)
    # 去重且保序
    seen, uniq = set(), []
    for p in out:
        a = os.path.abspath(p)
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_models:
        return cmd_list_models()
    if args.list_langs:
        return cmd_list_langs()
    if args.check:
        return cmd_check()
    if not args.inputs:
        build_parser().print_help()
        return 2

    files = expand_inputs(args.inputs)
    if not files:
        print("没有找到可处理的媒体文件。", file=sys.stderr)
        return 2

    # 准备模型（只下载/加载一次，批量时复用）
    repo = resolve_model(args.model)
    try:
        if os.path.isdir(repo):
            # 用户直接给了本地目录
            model_path = repo
            if not quiet:
                print(f"[模型] 使用本地目录：{model_path}")
        else:
            model_path = ensure_model(
                repo,
                cache_dir=args.model_dir,
                mirror=not args.no_mirror,
                verbose=not args.quiet,
            )
    except Exception as e:
        print(f"模型准备失败：{e}", file=sys.stderr)
        print("可加 --no-mirror 重试，或先用 --model small 验证流程。", file=sys.stderr)
        return 1

    opts = TranscribeOptions(
        model=repo,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        task=args.task,
        beam_size=args.beam_size,
        vad=not args.no_vad,
        initial_prompt=args.prompt,
    )

    total_t0 = time.time()
    results = []

    # 准备翻译器（只加载一次，批量时复用）
    translator = None
    if args.translate:
        tgt = norm_lang(args.translate) or "en"
        src_lang = norm_lang(args.src_lang or args.language or "zh") or "zh"
        if tgt == src_lang and not args.bilingual:
            print(f"[翻译] 目标语言与源语言相同（{lang_label(tgt)}），跳过翻译", file=sys.stderr)
        else:
            gloss = ""
            if args.mt_glossary:
                gf = args.mt_glossary
                if os.path.isfile(gf):
                    with open(gf, encoding="utf-8") as f:
                        gloss = f.read()
                    if not args.quiet:
                        n = len([l for l in gloss.splitlines() if l.strip() and not l.startswith("#")])
                        print(f"[翻译] 载入术语表 {gf}（{n} 条）")
                else:
                    gloss = gf  # 直接当字符串用
            topts = TranslateOptions(
                src=src_lang,
                dst=tgt,
                backend=args.mt_backend,
                model_dir=args.mt_model_dir,
                batch_size=args.mt_batch,
                glossary=gloss,
                context=args.mt_context or "",
                api_base=args.api_base or "",
                api_key=args.api_key or "",
                llm_model=args.llm_model,
            )
            try:
                translator = make_translator(topts, verbose=not args.quiet)
            except Exception as e:
                print(f"翻译器初始化失败：{e}", file=sys.stderr)
                return 1

    if len(files) == 1:
        tr = Transcriber(opts, verbose=not args.quiet, model_path=model_path)
        results.append(
            process_one(files[0], args, transcriber=tr, translator=translator,
                        quiet=args.quiet, model_path=model_path)
        )
    else:
        # 批量：模型共享，串行推理（GPU 上并行反而争抢显存）
        print(f"共 {len(files)} 个文件待处理\n")
        tr = Transcriber(opts, verbose=False, model_path=model_path)
        tr.load()
        for i, f in enumerate(files, 1):
            print(f"\n### [{i}/{len(files)}]")
            results.append(
                process_one(f, args, transcriber=tr, translator=translator,
                            quiet=args.quiet, model_path=model_path)
            )

    ok = sum(1 for r in results if r.get("ok"))
    fail = len(results) - ok

    print(f"\n{'=' * 58}")
    print(f"完成：成功 {ok}  失败 {fail}  总耗时 {time.time() - total_t0:.1f}s")
    if fail:
        for r in results:
            if not r.get("ok"):
                print(f"  x {os.path.basename(r['input'])}: {r.get('error')}")
    print("=" * 58)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
