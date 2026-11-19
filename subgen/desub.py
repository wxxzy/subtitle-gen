"""硬字幕遮盖（去除烧进画面的字幕）。

━━━ 先说清楚能做什么、不能做什么 ━━━

**不能真正"删除"**。硬字幕已经烧进像素了，原始画面信息不存在了。
本模块做的是**遮盖**：把指定区域糊掉。

━━━ 方案选择（用真实素材逐帧对比过） ━━━

  blur 模糊    ✅ 默认。字迹完全糊掉、看不出内容，观感最自然
  pixelate     ✅ 备选。最彻底（绝对看不出原文），但马赛克风格明显
  delogo       ❌ 实测留下**竖向条纹伪影**，比原字幕还难看
  box 黑条     ❌ 半透明黑条对白字无效，字反而更清楚

**关于 delogo 的坑（值得记）**：它靠"区域四周边缘像素插值"填充，
在窄条上效果不错；但字幕带通常很宽（几百像素），
左右两侧采样点跨度太大，插值会形成规则的竖向条纹。
所以不采用它作为默认方案。

**效果取决于背景**：字幕压在大面积纯色/虚化背景上几乎完美；
压在人脸、细密纹理上会留可见痕迹 —— 这是遮盖的固有限制。

━━━ 关于位置 ━━━

遮盖需要具体的像素坐标（ffmpeg 滤镜参数不支持 iw/ih 表达式，这点踩过坑）。
所以提供自动检测：抽样多帧，找"高亮像素密集的横向条带"。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .audio import FFmpeg, FFmpegNotFound

# 遮盖方式
METHODS = ("blur", "pixelate", "delogo", "box")

# boxblur 的色度半径上限实测是 13（不是文档常说的 18），
# 而且 luma:chroma 两个参数都要在范围内，否则直接报 "Invalid chroma_param"。
MAX_BLUR = 13


@dataclass
class DetectResult:
    """检测结果。带置信度，便于界面提示用户复核。"""

    region: Optional[Region]
    confidence: float = 0.0        # 0~1
    reason: str = ""
    candidates: List[Region] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "region": self.region.to_dict() if self.region else None,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass
class Region:
    """一个矩形遮盖区（像素坐标）。"""

    x: int
    y: int
    w: int
    h: int

    def to_delogo(self) -> str:
        """生成 delogo 滤镜参数。注意必须用具体数值，不支持表达式。"""
        x1 = max(1, self.x)
        y1 = max(1, self.y)
        x2 = min(self.w - 1, self.w)
        return f"delogo=x={x1}:y={y1}:w={max(4, x2)}:h={max(4, self.h)}"

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def clamp(self, frame_w: int, frame_h: int) -> "Region":
        """裁到画面范围内，并保证最小尺寸。"""
        x = max(0, min(int(self.x), max(0, frame_w - 8)))
        y = max(0, min(int(self.y), max(0, frame_h - 8)))
        w = max(8, min(int(self.w), frame_w - x))
        h = max(8, min(int(self.h), frame_h - y))
        return Region(x=x, y=y, w=w, h=h)


# 常用预设（按画面比例，不依赖具体分辨率）
PRESETS: Dict[str, Dict[str, float]] = {
    # 底部居中单行字幕（最常见）
    "bottom":      {"x": 0.04, "y": 0.680, "w": 0.92, "h": 0.075},
    # 底部双行
    "bottom2":     {"x": 0.04, "y": 0.650, "w": 0.92, "h": 0.115},
    # 底部靠下（避开画面主体）
    "bottom_low":  {"x": 0.04, "y": 0.760, "w": 0.92, "h": 0.075},
    # 顶部标题
    "top":         {"x": 0.04, "y": 0.075, "w": 0.92, "h": 0.065},
}


def preset_regions(name: str, width: int, height: int) -> List[Region]:
    """按预设名和画面尺寸得到遮盖区。"""
    p = PRESETS.get(name)
    if not p:
        return []
    return [Region(
        x=int(width * p["x"]),
        y=int(height * p["y"]),
        w=int(width * p["w"]),
        h=int(height * p["h"]),
    )]


# ---------------------------------------------------------------- 自动检测

def detect_subtitle_region(
    media: str,
    *,
    samples: int = 10,
    threshold: int = 0,
    min_rows: int = 6,
    band_from: float = 0.35,
    band_to: float = 0.98,
) -> DetectResult:
    """自动检测硬字幕位置（返回底部字幕区）。

    做法：均匀抽若干帧，逐帧找"高亮像素密集的高度带"，再汇总投票。

    为什么用**自适应阈值**而不是固定值（早期版本的坑）：
    素材背景明暗差异很大（天空、白衣、暗色室内），
    固定阈值 235 在亮背景上会把背景误判成文字、在暗场景上又完全检不出。
    改为按每帧自身的亮度分布取高分位（默认 97%），适应性好得多。

    只在 band_from~band_to 内找（默认下半部）——
    顶部通常是标题/水印，不属于"要去掉的台词字幕"。

    返回 None 表示没检测到（可能真的没有硬字幕）。
    """
    try:
        ff = FFmpeg()
    except FFmpegNotFound:
        return DetectResult(None, 0.0, "未找到 ffmpeg")

    w, h = _size(ff, media)
    if not w or not h:
        return DetectResult(None, 0.0, "读不到视频尺寸")

    dur = ff.duration(media) or 0
    if dur <= 0:
        return DetectResult(None, 0.0, "读不到视频时长")

    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return DetectResult(None, 0.0, "缺少 Pillow，无法分析画面")

    times = [dur * (i + 1) / (samples + 1) for i in range(samples)]
    tmpd = tempfile.mkdtemp(prefix="subgen_detect_")
    frames: List[str] = []
    try:
        for i, t in enumerate(times):
            p = os.path.join(tmpd, f"f{i}.png")
            r = subprocess.run(
                [ff.ffmpeg, "-hide_banner", "-v", "error", "-y",
                 "-ss", f"{t:.2f}", "-i", media, "-frames:v", "1",
                 "-vf", "scale=360:-1", p],
                capture_output=True, timeout=60,
            )
            if r.returncode == 0 and os.path.isfile(p):
                frames.append(p)
        if len(frames) < 2:
            return DetectResult(None, 0.0, "抽帧失败")
        r = _find_band(frames, w, h, threshold, min_rows, band_from, band_to)
        return r or DetectResult(None, 0.0, "未检测到字幕带")
    finally:
        for p in frames:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpd)
        except OSError:
            pass


def _size(ff: FFmpeg, media: str) -> Tuple[int, int]:
    try:
        info = ff.probe(media)
        for s in info.get("streams", []):
            if s.get("codec_type") == "video" and s.get("width"):
                return int(s["width"]), int(s["height"])
    except Exception:
        pass
    return 0, 0


def _frame_row_profile(im, threshold: int) -> List[float]:
    """单帧的逐行"文字特征强度"。

    文字行有两个特征，组合起来比单看亮度可靠：
      1. 有很亮的像素（白字）
      2. 该行明暗对比强（文字的笔画造成高频变化）
    这里用「该行超过自适应阈值的像素数」衡量，阈值按帧自身分布定。
    """
    px = im.load()
    W, H = im.size

    # 自适应阈值：取全帧亮度的高分位
    if threshold <= 0:
        hist = im.histogram()
        total = sum(hist)
        acc = 0
        thr = 200
        for v in range(255, -1, -1):
            acc += hist[v]
            if acc >= total * 0.03:      # 最亮的 3%
                thr = max(150, v)
                break
        threshold = thr

    prof: List[float] = []
    for y in range(H):
        c = 0
        for x in range(0, W, 2):
            if px[x, y] > threshold:
                c += 1
        prof.append(float(c))
    return prof


def _find_band(frames: List[str], full_w: int, full_h: int,
               threshold: int, min_rows: int,
               band_from: float, band_to: float) -> Optional[DetectResult]:
    """在多帧里投票找出字幕带。返回带置信度的结果。"""
    from PIL import Image

    imgs = []
    for p in frames:
        try:
            imgs.append(Image.open(p).convert("L"))
        except Exception:
            pass
    if not imgs:
        return None

    W, H = imgs[0].size
    profiles = [_frame_row_profile(im, threshold) for im in imgs]

    # 逐帧归一化：把每行的计数除以该帧的最大值，
    # 这样不同视频、不同场景的绝对亮度差异不影响比较
    norm = []
    for prof in profiles:
        mx = max(prof) or 1.0
        norm.append([v / mx for v in prof])

    y0i, y1i = int(H * band_from), int(H * band_to)

    # 投票：统计"这一行在多少帧里属于文字特征行"
    votes = [0] * H
    for prof, raw in zip(norm, profiles):
        abs_th = max(6.0, W * 0.05)
        for y in range(y0i, y1i):
            # 归一化后强度够高 **且** 绝对像素数够多，才算"有文字"
            if prof[y] > 0.35 and raw[y] >= abs_th:
                votes[y] += 1

    need = max(2, len(imgs) // 3)
    runs: List[Tuple[int, int]] = []
    start = None
    for y in range(y0i, y1i):
        if votes[y] >= need:
            if start is None:
                start = y
        else:
            if start is not None and y - start >= 4:
                runs.append((start, y))
            start = None
    if start is not None and y1i - start >= 4:
        runs.append((start, y1i))

    if not runs:
        return DetectResult(None, 0.0, "未找到符合特征的横向条带")

    scale = full_h / H

    def mk(run: Tuple[int, int]) -> Region:
        a, b = run
        pad = max(5, int(H * 0.015))
        y0 = max(0, int((a - pad) * scale))
        y1 = min(full_h, int((b + pad) * scale))
        return Region(x=int(full_w * 0.02), y=y0,
                      w=int(full_w * 0.96), h=max(10, y1 - y0))

    cands = [mk(r) for r in sorted(runs, key=lambda r: r[1], reverse=True)]

    # ★ 关键取舍：多个候选带时**优先取最靠下的那个**。
    #
    # 为什么不是"取最长的"（早期版本的坑）：实测白猫的高光区、
    # 浅色衣服的亮部都会形成密集带，且往往比字幕带更大更长。
    # 但台词字幕几乎总在画面下方 —— 这个先验比"谁更长"可靠得多。
    bottom_run = max(runs, key=lambda r: r[1])
    longest_run = max(runs, key=lambda r: r[1] - r[0])
    chosen_run = bottom_run if (bottom_run[1] - bottom_run[0]) >= 6 else longest_run
    region = mk(chosen_run)

    # 置信度：越靠近画面底部、条带越厚、命中帧越多，越可信
    h_ratio = chosen_run[0] / H                      # 起点在画面中的相对高度
    thick = chosen_run[1] - chosen_run[0]
    hit = max(votes[chosen_run[0]:chosen_run[1]] or [0])
    conf = 0.0
    conf += min(0.45, h_ratio * 0.55)                # 靠下加分
    conf += min(0.25, thick / H * 3.0)               # 厚度加分
    conf += min(0.30, hit / max(1, len(imgs)) * 0.35)  # 命中帧加分
    conf = round(min(1.0, conf), 2)

    reason = ("条带位于画面下方" if h_ratio > 0.6 else "条带位置偏上，可能不是字幕")
    if len(runs) > 1:
        reason += f"（另有 {len(runs)-1} 个候选区）"

    return DetectResult(region, conf, reason, cands)


def detect_regions(media: str, *, include_top: bool = False) -> List[Region]:
    """检测所有候选区域（底字幕 + 可选顶部标题）。"""
    out: List[Region] = []
    r = detect_subtitle_region(media)
    if r and r.region:
        out.append(r.region)
    if include_top:
        t = detect_subtitle_region(media, band_from=0.02, band_to=0.22)
        if t and t.region:
            out.append(t.region)
    return out


# ---------------------------------------------------------------- 处理

def build_filter(regions: Sequence[Region], method: str = "blur",
                 frame_w: int = 0, frame_h: int = 0,
                 strength: int = 0) -> str:
    """把多个区域合成一条滤镜链。

    method:
      blur     默认。用"降采样+放大"做深度模糊，strength 为降采样倍数
               （默认 40，越大越糊；超过 60 会变成纯色块）
      pixelate 马赛克
      delogo   边缘插值（宽区域会出竖条纹，仅作备选）
      box      半透明黑条（对浅色背景的字无效，仅作备选）
    """
    parts: List[str] = []
    for k, r in enumerate(regions):
        rr = r.clamp(frame_w, frame_h) if (frame_w and frame_h) else r
        if rr.w < 8 or rr.h < 8:
            continue
        x, y, w, h = rr.x, rr.y, rr.w, rr.h

        if method == "pixelate":
            bw = max(4, w // 24)
            bh = max(2, max(2, h // 6))
            parts.append(
                f"split[bg{k}][mz{k}];"
                f"[mz{k}]crop={w}:{h}:{x}:{y},scale={bw}:{bh},"
                f"scale={w}:{h}:flags=neighbor[px{k}];"
                f"[bg{k}][px{k}]overlay={x}:{y}"
            )
        elif method == "delogo":
            parts.append(
                f"delogo=x={x + 4}:y={y + 4}:w={max(8, w - 8)}:h={max(8, h - 8)}"
            )
        elif method == "box":
            parts.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black@0.85:t=fill")
        else:
            # blur（默认）：用「强降采样 + 放大」实现深度模糊。
            #
            # 为什么不用 boxblur（踩过的坑）：boxblur 的色度半径上限只有 13，
            # 对这种「粗体大白字 + 描边」根本糊不透，字迹依然清晰可读。
            # 降采样到 1/40 再放大回来，等效模糊半径远超 boxblur 上限，
            # 实测能把字完全糊掉，同时保留背景明暗过渡，观感自然。
            div = max(8, int(strength) if strength else 40)
            bw = max(2, w // div)
            bh = max(1, h // div)
            parts.append(
                f"split[bg{k}][mz{k}];"
                f"[mz{k}]crop={w}:{h}:{x}:{y},"
                f"scale={bw}:{bh},scale={w}:{h}:flags=bilinear[bl{k}];"
                f"[bg{k}][bl{k}]overlay={x}:{y}"
            )
    return ",".join(parts)


def remove_subtitles(
    src: str,
    dst: str,
    regions: Sequence[Region],
    *,
    method: str = "blur",
    encoder: str = "auto",
    crf: int = 20,
    preset: str = "veryfast",
    strength: int = 0,
    copy_audio: bool = True,
    progress_cb=None,
    should_cancel=None,
) -> str:
    """把 src 里的字幕区遮盖掉，输出到 dst。

    encoder: auto（有 N 卡用 nvenc，否则 libx264）/ libx264 / h264_nvenc / hevc_nvenc
    """
    ff = FFmpeg()
    w, h = ff.video_size(src) if _is_video(src) else (0, 0)
    vf = build_filter(regions, method=method, frame_w=w, frame_h=h, strength=strength)
    if not vf:
        raise ValueError("没有有效的遮盖区域")

    enc = _pick_encoder(ff, encoder)
    cmd = [ff.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, "-vf", vf]

    if enc == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(crf), "-rc", "vbr"]
    elif enc == "hevc_nvenc":
        cmd += ["-c:v", "hevc_nvenc", "-preset", "p4", "-cq", str(crf), "-rc", "vbr"]
    else:
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]

    cmd += ["-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "128k"]
    # 保留原始元数据里的旋转信息（竖屏视频很依赖这个）
    cmd += ["-map_metadata", "0", "-movflags", "+faststart", dst]

    if should_cancel and should_cancel():
        raise RuntimeError("已取消")

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError(f"遮盖处理失败：{(r.stderr or '')[-500:]}")
    return dst


def _is_video(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in (".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv",
                   ".webm", ".m4v", ".ts", ".mpg", ".mpeg")


def _pick_encoder(ff: FFmpeg, want: str) -> str:
    if want and want != "auto":
        return want
    # N 卡走 GPU 编码，速度快很多
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            return "h264_nvenc"
    except Exception:
        pass
    return "libx264"


def default_output(src: str, *, suffix: str = ".clean") -> str:
    """生成默认输出路径（与源同目录）。

    注意要**避免重复后缀**：如果对已处理过的文件再处理一次，
    不能产出 xxx.clean.clean.mp4（踩过这个坑）。
    """
    base, ext = os.path.splitext(src)
    if base.endswith(suffix):
        return f"{base}{ext or '.mp4'}"
    return f"{base}{suffix}{ext or '.mp4'}"
