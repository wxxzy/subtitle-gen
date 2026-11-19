"""校对工作台的桥接层。

职责：
  - 把视频/音频通过本地 HTTP 服务（支持 Range）提供给界面播放
  - 读写字幕文件
  - 打开独立的工作台窗口

为什么要独立窗口而不是嵌在主界面里：
校对是个"沉浸式"的活，需要大的播放区和长的字幕列表。
做成独立窗口可以用满屏幕，也和"生成 → 校对"两阶段的工作流契合。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, Dict, List, Optional

from .mediaserve import get_server
from .subio import SubCue, find_subtitles, read_subtitle, write_subtitle

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_HTML = os.path.join(APP_DIR, "webui", "review.html")

# 复用主界面的版本号工具：WebView2 会强缓存旧页面，
# 工作台窗口同理，不加版本号会出现"改了 review.html 但看到的还是旧界面"。
from .gui_main import versioned_url  # noqa: E402

# 传给工作台窗口的初始状态（窗口创建时前端会来取）
_INIT: Dict[str, Any] = {}
_INIT_LOCK = threading.Lock()


def _media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg"):
        return "video"
    return "audio"


def prepare(video_path: str, sub_path: str = "") -> Dict[str, Any]:
    """准备一次校对会话：注册媒体、找字幕、记录初始状态。"""
    video_path = os.path.abspath(video_path)
    url = ""
    if os.path.isfile(video_path) and _media_type(video_path) == "video":
        url = get_server().url_for(video_path)
    elif os.path.isfile(video_path):
        url = get_server().url_for(video_path)

    if not sub_path:
        cands = find_subtitles(video_path)
        # 优先中文/原文，其次任意
        pick = ""
        for c in cands:
            n = os.path.basename(c).lower()
            if n.endswith(".zh.srt") or n.endswith(".srt") and ".en." not in n:
                pick = c
                break
        sub_path = pick or (cands[0] if cands else "")

    with _INIT_LOCK:
        _INIT.clear()
        _INIT.update({
            "videoPath": video_path,
            "videoUrl": url,
            "subPath": os.path.abspath(sub_path) if sub_path else "",
            "mediaType": _media_type(video_path),
        })
    return dict(_INIT)


class ReviewApi:
    """工作台窗口的 JS API。

    与主界面的 GuiApi 一样，**不能持有任何 .NET 对象**
    （pywebview 会递归扫描公开属性，导致 .NET 属性链无限递归刷警告）。
    """

    def __init__(self) -> None:
        # 波形缓存：key 是 (路径, 起点, 时长, 分辨率)。
        # 拖动/缩放时间轴会反复请求同一段，缓存能避免重复调用 ffmpeg。
        self._wave_cache: Dict[Tuple, List[int]] = {}
        self._wave_lock = threading.Lock()

    # ---------------------------------------------------------- 波形
    def get_waveform(self, path: str = "", start: float = 0.0,
                     duration: float = 0.0, pps: int = 100) -> Dict[str, Any]:
        """取指定区间的波形峰值。区间不传时取整条音频。"""
        from .waveform import extract_peaks

        if not path:
            with _INIT_LOCK:
                path = _INIT.get("videoPath", "")
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "找不到媒体文件"}

        # 时长上限：一次最多提 10 分钟，避免超长音频把界面拖死
        dur = float(duration or 0)
        if dur <= 0:
            try:
                from .audio import FFmpeg
                total = FFmpeg().duration(path)
                dur = max(0.0, total - start)
            except Exception:
                dur = 60.0
        dur = min(dur, 600.0)
        pps = max(10, min(400, int(pps or 100)))

        key = (os.path.abspath(path), round(start, 3), round(dur, 3), pps)
        with self._wave_lock:
            if key in self._wave_cache:
                peaks = self._wave_cache[key]
            else:
                peaks = []
        if not peaks:
            try:
                peaks = extract_peaks(path, start, dur, pps=pps)
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if not peaks:
                return {"ok": False, "error": "该文件没有可用的音轨"}
            with self._wave_lock:
                # 缓存不要无限增长
                if len(self._wave_cache) > 40:
                    self._wave_cache.clear()
                self._wave_cache[key] = peaks

        mx = max(peaks) or 1
        return {
            "ok": True,
            "start": start,
            "duration": dur,
            "pps": pps,
            "peak": mx,
            "peaks": peaks,
        }

    # ---------------------------------------------------------- 初始化
    def review_init(self) -> Dict[str, Any]:
        """前端启动时来取初始状态。"""
        with _INIT_LOCK:
            return dict(_INIT)

    # ---------------------------------------------------------- 文件选择
    def pick_subtitle(self) -> Optional[Dict[str, Any]]:
        """选一个字幕文件并返回解析结果。"""
        r = _pick_file(
            "字幕文件 (*.srt;*.vtt)", "所有文件 (*.*)"
        )
        if not r:
            return None
        return self.load_subtitle(r)

    def load_subtitle(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            cues = read_subtitle(path)
            return {
                "path": os.path.abspath(path),
                "cues": [c.to_dict() for c in cues],
            }
        except Exception:
            return None

    def open_video(self) -> Optional[Dict[str, Any]]:
        """选一个视频/音频文件，返回可播放的 URL。"""
        r = _pick_file(
            "视频 (*.mp4;*.mkv;*.mov;*.avi;*.flv;*.wmv;*.webm;*.m4v;*.ts)",
            "音频 (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus)",
            "所有文件 (*.*)  (媒体文件)",
        )
        if not r:
            return None
        u = get_server().url_for(r)
        # 顺手找同名字幕
        subs = find_subtitles(r)
        return {
            "path": os.path.abspath(r),
            "url": u,
            "mediaType": _media_type(r),
            "siblings": [os.path.abspath(s) for s in subs],
        }

    # ---------------------------------------------------------- 保存
    def save_subtitle(self, path: str, cues: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把编辑后的字幕写回文件。写前自动备份一次，防止误操作丢内容。"""
        try:
            if not path:
                return {"ok": False, "error": "路径为空"}
            items: List[SubCue] = []
            for i, c in enumerate(cues or [], 1):
                items.append(SubCue(
                    start=float(c.get("start", 0.0)),
                    end=float(c.get("end", 0.0)),
                    text=str(c.get("text", "")),
                    index=i,
                ))
            # 按时间排序并修掉交叉，保证输出合法
            items.sort(key=lambda x: x.start)
            for i in range(len(items) - 1):
                if items[i].end > items[i + 1].start + 1e-6:
                    items[i].end = max(items[i].start + 0.1, items[i + 1].start - 0.02)
            for it in items:
                if it.end <= it.start:
                    it.end = it.start + 0.5

            # 备份原文件（只留一份，避免堆积）
            if os.path.isfile(path):
                try:
                    bak = path + ".bak"
                    with open(path, "rb") as src, open(bak, "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass

            write_subtitle(path, items)
            return {"ok": True, "path": os.path.abspath(path)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def save_as(self, cues: List[Dict[str, Any]]) -> Dict[str, Any]:
        """另存为。"""
        r = _save_file("字幕文件 (*.srt)", "所有文件 (*.*)")
        if not r:
            return {"ok": False, "error": "未选择路径"}
        return self.save_subtitle(r, cues)

    def reveal(self, path: str) -> Dict[str, Any]:
        """在资源管理器中定位文件。"""
        try:
            p = os.path.abspath(path)
            if os.path.isfile(p):
                import subprocess
                subprocess.Popen(["explorer", "/select,", p])
                return {"ok": True}
            d = os.path.dirname(p) if os.path.splitext(p)[1] else p
            if os.path.isdir(d):
                os.startfile(d)  # noqa: S606
                return {"ok": True}
            return {"ok": False, "error": "路径不存在"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 对话框

def _pick_file(*file_types: str) -> str:
    """打开文件选择对话框。取窗口引用时容错，避免工作台被孤立。"""
    try:
        import webview
        w = _window()
        if w is None:
            return ""
        r = w.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                 file_types=file_types)
        if not r:
            return ""
        return r[0] if isinstance(r, (list, tuple)) else str(r)
    except Exception:
        return ""


def _save_file(*file_types: str) -> str:
    try:
        import webview
        w = _window()
        if w is None:
            return ""
        r = w.create_file_dialog(webview.SAVE_DIALOG, save_filename="字幕.srt",
                                 file_types=file_types)
        if not r:
            return ""
        return r[0] if isinstance(r, (list, tuple)) else str(r)
    except Exception:
        return ""


# 工作台自己的窗口引用（同样放模块级，避开 pywebview 的属性扫描）
_REVIEW_WIN: Dict[str, Any] = {"w": None}


def set_review_window(w) -> None:
    _REVIEW_WIN["w"] = w


def _window():
    """优先用工作台窗口，其次用主界面窗口。"""
    if _REVIEW_WIN.get("w") is not None:
        return _REVIEW_WIN["w"]
    try:
        from .gui import get_window
        return get_window()
    except Exception:
        return None


def open_review(video_path: str, sub_path: str = "") -> bool:
    """在独立窗口里打开校对工作台。"""
    if not os.path.isfile(REVIEW_HTML):
        return False
    prepare(video_path, sub_path)

    import webview
    api = ReviewApi()
    w = webview.create_window(
        "字幕校对 · " + os.path.basename(video_path),
        versioned_url(REVIEW_HTML),
        js_api=api,
        width=1360,
        height=880,
        min_size=(980, 620),
        background_color="#eef1f6",
        text_select=True,   # 校对要能选中文本
    )
    set_review_window(w)
    return True
