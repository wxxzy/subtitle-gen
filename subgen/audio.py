"""音视频处理：ffmpeg/ffprobe 封装。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".amr",
}
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp", ".rmvb",
}


class FFmpegNotFound(RuntimeError):
    pass


# 字幕轨的 language 元数据要 ISO 639-2/B 三字母码，播放器才认。
# 只写 zh 这类两字母码的话，MKV 里会出现 "und"（未定义）轨道，菜单里显示不出语言名。
ISO3 = {
    "zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor", "fr": "fre",
    "de": "ger", "es": "spa", "ru": "rus", "pt": "por", "it": "ita",
    "ar": "ara", "th": "tha", "vi": "vie", "id": "ind", "hi": "hin",
    "nl": "dut", "pl": "pol", "tr": "tur", "sv": "swe", "da": "dan",
    "fi": "fin", "cs": "cze", "uk": "ukr", "he": "heb", "ms": "may",
    "yue": "chi", "und": "und",
}

# 轨道显示名（播放器字幕菜单里看到的名字）
TRACK_TITLES = {
    "zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "es": "Español", "ru": "Русский",
    "pt": "Português", "it": "Italiano", "ar": "العربية", "th": "ไทย",
    "vi": "Tiếng Việt", "id": "Indonesia",
}


def _iso3(code: str) -> str:
    if not code:
        return "und"
    return ISO3.get(code.strip().lower(), code.strip().lower()[:3] or "und")


def _track_title(code: str) -> str:
    return TRACK_TITLES.get((code or "").strip().lower(), "")


def _which(name: str) -> Optional[str]:
    """查找可执行文件。优先 PATH，其次常见安装位置。"""
    p = shutil.which(name)
    if p:
        return p
    for c in (
        rf"C:\ffmpeg\bin\{name}.exe",
        rf"C:\Program Files\ffmpeg\bin\{name}.exe",
        rf"C:\dev\ffmpeg\bin\{name}.exe",
        rf"C:\Users\{os.environ.get('USERNAME','')}\scoop\shims\{name}.exe",
        rf"C:\ProgramData\chocolatey\bin\{name}.exe",
    ):
        if os.path.isfile(c):
            return c
    return None


class FFmpeg:
    def __init__(self) -> None:
        self.ffmpeg = _which("ffmpeg")
        self.ffprobe = _which("ffprobe")
        if not self.ffmpeg:
            raise FFmpegNotFound(
                "未找到 ffmpeg。请安装后重试：\n"
                "  winget install Gyan.FFmpeg\n"
                "  或到 https://www.gyan.dev/ffmpeg/builds/ 下载后把 bin 目录加入 PATH"
            )

    # ------------------------------------------------------------ probe

    def probe(self, path: str) -> dict:
        """返回媒体信息。ffprobe 不在时用 ffmpeg -i 解析兜底。"""
        if self.ffprobe:
            cmd = [
                self.ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ]
            try:
                out = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
                if out.returncode == 0 and out.stdout.strip():
                    return json.loads(out.stdout)
            except Exception:
                pass
        return self._probe_fallback(path)

    def _probe_fallback(self, path: str) -> dict:
        out = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        info: dict = {"format": {}, "streams": []}
        for line in (out.stderr or "").splitlines():
            line = line.strip()
            if line.startswith("Duration:"):
                hms = line.split("Duration:")[1].split(",")[0].strip()
                try:
                    h, m, s = hms.split(":")
                    info["format"]["duration"] = str(int(h) * 3600 + int(m) * 60 + float(s))
                except Exception:
                    pass
        return info

    def duration(self, path: str) -> float:
        info = self.probe(path)
        d = info.get("format", {}).get("duration")
        if d:
            try:
                return float(d)
            except Exception:
                pass
        for s in info.get("streams", []):
            if s.get("duration"):
                try:
                    return float(s["duration"])
                except Exception:
                    pass
        return 0.0

    def has_audio(self, path: str) -> bool:
        info = self.probe(path)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))

    def video_size(self, path: str) -> Tuple[int, int]:
        """返回 (width, height)，判断不出时给 1920x1080。"""
        info = self.probe(path)
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width"), s.get("height")
                if w and h:
                    # 竖屏视频也按实际方向处理
                    return int(w), int(h)
        return 1920, 1080

    # ------------------------------------------------------------ extract

    def extract_audio(
        self,
        src: str,
        dst: str | None = None,
        *,
        sample_rate: int = 16000,
        mono: bool = True,
        denoise: bool = False,
        start: float | None = None,
        duration: float | None = None,
    ) -> str:
        """抽音轨为 16 kHz 单声道 wav —— Whisper 的原生输入规格。"""
        if dst is None:
            fd, dst = tempfile.mkstemp(suffix=".wav", prefix="subgen_")
            os.close(fd)

        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if start is not None:
            cmd += ["-ss", str(start)]
        cmd += ["-i", src]
        if duration is not None:
            cmd += ["-t", str(duration)]
        cmd += ["-vn", "-map", "0:a:0?", "-ac", "1" if mono else "2",
                "-ar", str(sample_rate), "-acodec", "pcm_s16le"]
        if denoise:
            # 轻度降噪 + 动态压缩，对带底噪的录屏/实地录音有改善
            cmd += ["-af", "afftdn=nf=-25,dynaudnorm=f=200:g=5"]
        cmd += [dst]

        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError(f"抽取音轨失败：{(r.stderr or '')[-600:]}")
        return dst

    # ------------------------------------------------------------ burn-in

    def burn(self, video: str, subtitle: str, out: str, *, font: str | None = None) -> str:
        """把字幕烧进画面（硬字幕）。"""
        p = os.path.abspath(subtitle).replace("\\", "/").replace(":", r"\:")
        style = f"FontName={font}," if font else ""
        vf = f"subtitles='{p}':force_style='{style}FontSize=20'"
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", video, "-vf", vf, "-c:a", "copy",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", out,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"烧录字幕失败：{(r.stderr or '')[-600:]}")
        return out

    def mux_soft(self, video: str, subtitle: str, out: str, *, lang: str = "chi") -> str:
        """把字幕作为软字幕轨道封装进 MKV（不损画质，可开关）。"""
        return self.mux_multi(video, [(subtitle, lang)], out)

    def mux_multi(self, video: str, tracks: list, out: str,
                  *, default_lang: str = "") -> str:
        """把多条字幕轨封装进同一个 MKV，播放器可自由切换。

        tracks: [(字幕文件路径, 语言代码), ...]

        这才是「多语言合在一起」的正确形态——不是把两种语言塞进同一条字幕，
        而是各语言一条独立轨道，观众按需要开关。相比分开的文件，好处是
        只传一个文件、播放器里直接切、语言可任意组合显示。
        """
        if not tracks:
            raise ValueError("至少需要一条字幕轨")

        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video]
        for path, _ in tracks:
            cmd += ["-i", path]

        # 第一条字幕轨强制转为 SRT：混用 ass/srt 输入时，若直接 -c copy
        # 会出现 MKV 内轨道类型不一致、部分播放器不识别的问题
        cmd += ["-map", "0"]
        for i in range(len(tracks)):
            cmd += ["-map", f"{i + 1}:0"]
        cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "srt"]

        for i, (_, lang) in enumerate(tracks):
            code = _iso3(lang)
            cmd += [f"-metadata:s:s:{i}", f"language={code}"]
            label = _track_title(lang)
            if label:
                cmd += [f"-metadata:s:s:{i}", f"title={label}"]

        # 默认轨：优先用调用方指定，否则第一条
        default_idx = 0
        if default_lang:
            for i, (_, lang) in enumerate(tracks):
                if lang == default_lang:
                    default_idx = i
                    break
        for i in range(len(tracks)):
            cmd += [f"-disposition:s:{i}", "default" if i == default_idx else "0"]

        cmd += [out]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError(f"封装多轨软字幕失败：{(r.stderr or '')[-600:]}")
        return out


def is_media_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (AUDIO_EXTS | VIDEO_EXTS)


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS
