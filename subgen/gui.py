"""GUI 桥接层 —— 把前端的调用转成 subgen 核心的逻辑。

职责边界：
  前端(JS)  ->  本模块  ->  subgen.tasks / subgen.models / subgen.audio
           <-          <-

本模块**不做任何处理逻辑**，只负责：
  1. 参数转换（JS 的 dict -> RunOptions）
  2. 后台线程调度（GUI 不能被阻塞）
  3. 进度事件转发到网页
  4. 文件对话框、设置持久化、打开目录这类桌面操作
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from .audio import AUDIO_EXTS, VIDEO_EXTS, FFmpeg, FFmpegNotFound, is_media_file
from .engine import Transcriber, TranscribeOptions
from .models import gpu_summary, ensure_model, resolve_model
from .reporter import CallbackReporter, Cancelled, Progress
from .tasks import RunOptions, run_one
from .translator import make_translator, norm_lang, TranslateOptions

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(APP_DIR, "models")
SETTINGS_FILE = os.path.join(APP_DIR, ".gui_settings.json")

MEDIA_EXTS = tuple(sorted(VIDEO_EXTS | AUDIO_EXTS))

# 持有窗口引用。
# 为什么不放在 GuiApi 实例上：pywebview 会递归扫描 js_api 对象的**所有**
# 公开属性来生成 JS 端调用清单，而 webview.Window 底层是 .NET 对象，
# 属性链（native.AccessibilityObject.Empty.Empty...）会无限递归，
# 日志里刷出一大串 "Error while processing"。放在模块级就不会被扫到。
_WINDOW_HOLDER: dict = {"w": None}

# 当前任务开始时刻，用于计算总用时（进度事件里带上，界面显示"已用 X"）
_JOB_T0: list = [0.0]


def set_window(w) -> None:
    """登记 pywebview 窗口，供 API 回调使用。"""
    _WINDOW_HOLDER["w"] = w


def get_window():
    return _WINDOW_HOLDER.get("w")


def _model_cached(repo: str, cache_dir: str) -> bool:
    """判断模型是否已下载到本地（用于区分"下载中"和"就绪"，给用户明确反馈）。"""
    d = os.path.join(cache_dir, repo.replace("/", "--"))
    f = os.path.join(d, "model.bin")
    try:
        return os.path.isfile(f) and os.path.getsize(f) > 1024
    except OSError:
        return False



class GuiApi:
    """暴露给 JS 的 API。方法名就是 JS 里 window.pywebview.api.<name> 的名字。

    重要：本类**不持有任何 .NET 对象**（窗口引用放在模块级 _WINDOW_HOLDER）。
    pywebview 会递归扫描 js_api 的所有公开属性来生成 JS 侧调用清单，
    而 window 底层是 .NET 对象，属性链会无限递归并刷出大量告警日志。
    """

    def __init__(self, window=None) -> None:
        if window is not None:
            set_window(window)
        self._job_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._reporter: Optional[CallbackReporter] = None
        self._transcriber: Optional[Transcriber] = None
        self._last_outputs: List[str] = []
        self._lock = threading.Lock()
        # 事件队列：后台线程写入，前端通过 poll_events 取走（见 _emit 的注释）
        self._events: List[Dict[str, Any]] = []
        self._ev_lock = threading.Lock()

    # ---------------------------------------------------------- 环境

    def get_env(self) -> Dict[str, Any]:
        """给界面顶部显示的环境信息。"""
        out: Dict[str, Any] = {"ffmpeg": True, "device": "cpu", "gpu_name": ""}
        try:
            FFmpeg()
        except FFmpegNotFound:
            out["ffmpeg"] = False
        try:
            info = gpu_summary()
            if info.get("count", 0) > 0:
                out["device"] = "cuda"
                out["gpu_name"] = (info.get("names") or ["GPU"])[0]
                out["compute_type"] = info.get("recommended_compute_type", "")
        except Exception:
            pass
        out["version"] = __import__("subgen", fromlist=["__version__"]).__version__
        return out

    # ---------------------------------------------------------- 文件

    def pick_files(self) -> List[str]:
        if not get_window():
            return []
        try:
            import webview
            r = get_window().create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=(
                    "媒体文件 (*.mp4;*.mkv;*.mov;*.avi;*.flv;*.wmv;*.webm;*.m4v;*.ts;"
                    "*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma)",
                    "视频 (*.mp4;*.mkv;*.mov;*.avi;*.flv;*.wmv;*.webm;*.m4v;*.ts)",
                    "音频 (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma)",
                    "所有文件 (*.*)",
                ),
            )
            return list(r) if r else []
        except Exception:
            return []

    def pick_folder(self) -> str:
        if not get_window():
            return ""
        try:
            import webview
            r = get_window().create_file_dialog(webview.FOLDER_DIALOG)
            if not r:
                return ""
            return r[0] if isinstance(r, (list, tuple)) else str(r)
        except Exception:
            return ""

    def list_media(self, folder: str) -> List[str]:
        """列出目录下的媒体文件（不递归，避免用户误选大目录卡住）。"""
        if not folder or not os.path.isdir(folder):
            return []
        out = []
        try:
            for name in sorted(os.listdir(folder)):
                p = os.path.join(folder, name)
                if os.path.isfile(p) and p.lower().endswith(MEDIA_EXTS):
                    out.append(p)
        except OSError:
            pass
        return out

    def resolve_dropped(self, names: List[str]) -> Dict[str, Any]:
        """拖放兜底。

        pywebview 的 HTML5 拖放拿不到完整路径（浏览器安全限制）。
        这里尝试两种补救：
          1. 若名字本身已是完整路径，直接用
          2. 否则在常见目录里按文件名搜索（限深度，避免全盘扫描）
        """
        paths: List[str] = []
        for n in (names or []):
            if os.path.isfile(n):
                paths.append(os.path.abspath(n))
        if paths:
            return {"paths": paths}

        # 在用户目录下浅层搜索
        found: List[str] = []
        targets = {n.lower() for n in (names or [])}
        roots = [
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/Videos"),
            os.path.expanduser("~/Documents"),
            os.path.expanduser("~/工作"),
        ]
        for root in roots:
            if not os.path.isdir(root) or not targets:
                continue
            for cur, dirs, files in os.walk(root):
                depth = cur[len(root):].count(os.sep)
                if depth > 2:
                    dirs[:] = []
                    continue
                for fn in files:
                    if fn.lower() in targets:
                        found.append(os.path.join(cur, fn))
                        targets.discard(fn.lower())
                if not targets:
                    break
            if not targets:
                break

        if found:
            return {"paths": found}
        return {"paths": [], "need_dialog": True}

    def push_dropped(self, paths: List[str]) -> None:
        """由拖放桥接调用：把真实路径推给前端。

        前端收到后走自己的 addPaths 流程（去重、探测时长、渲染）。
        """
        if not paths:
            return
        payload = json.dumps(list(paths), ensure_ascii=False)
        self._emit(f"window.onDroppedPaths && window.onDroppedPaths({payload})")

    # ---------------------------------------------------------- 校对工作台

    def open_review(self, video_path: str = "", sub_path: str = "") -> Dict[str, Any]:
        """打开字幕校对工作台（独立窗口）。

        不传参数时，若队列里已有文件就用第一个。
        这是一个**新窗口**，主界面保持原样，两者互不干扰。
        """
        try:
            from .review import open_review as _open
        except Exception as e:
            return {"ok": False, "error": f"工作台模块加载失败：{e}"}

        vp = video_path
        if not vp:
            # 优先选已完成的任务（有字幕可校对），否则用队列第一个
            with self._lock:
                done = [p for p in self._last_outputs]
            if done:
                vp = done[0]
            else:
                vp = ""
        if not vp or not os.path.isfile(vp):
            # 交给工作台自己去选文件
            pass

        try:
            ok = _open(vp, sub_path)
            if not ok:
                return {"ok": False, "error": "工作台界面文件缺失"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def list_outputs(self) -> List[Dict[str, Any]]:
        """列出最近产出的文件（前端用来决定校对哪个）。"""
        out = []
        for p in self._last_outputs:
            if os.path.isfile(p):
                out.append({"path": p, "name": os.path.basename(p),
                            "ext": os.path.splitext(p)[1].lower()})
        return out

    # ---------------------------------------------------------- 大模型配置

    def list_llm_providers(self) -> Dict[str, Any]:
        """返回内置的服务商预设，供界面下拉选择。"""
        from .translator import LLM_PROVIDERS, guess_provider, resolve_api
        opts = TranslateOptions()
        base, key, source, is_local = resolve_api(opts)
        cur = guess_provider(base)
        return {
            "ok": True,
            "providers": [
                {"id": k, "name": v["name"], "base": v["base"],
                 "model": v["model"], "key_url": v["key_url"], "note": v["note"]}
                for k, v in LLM_PROVIDERS.items()
            ],
            # 当前生效的配置（可能来自环境变量），界面用它回显
            "current": {"provider": cur, "base": base, "has_key": bool(key),
                        "key_source": source},
        }

    def test_llm(self, api_key: str = "", api_base: str = "",
                 llm_model: str = "") -> Dict[str, Any]:
        """测试大模型连接。配错端点是最常见的坑，先验一下省得跑一半才发现。"""
        try:
            from .translator import test_llm_connection
        except Exception as e:
            return {"ok": False, "error": f"模块加载失败：{e}"}
        opts = TranslateOptions(api_key=api_key or "", api_base=api_base or "",
                                llm_model=llm_model or "deepseek-chat")
        try:
            return test_llm_connection(opts)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def open_url(self, url: str) -> Dict[str, Any]:
        """用系统浏览器打开链接（用于「去申请 Key」按钮）。"""
        try:
            if not url or not url.startswith(("http://", "https://")):
                return {"ok": False, "error": "链接不合法"}
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------------------------------------------------------- 去字幕

    def detect_subtitle(self, video_path: str = "") -> Dict[str, Any]:
        """检测视频里的硬字幕位置（供界面填入遮盖区域）。"""
        try:
            from .desub import detect_subtitle_region
        except Exception as e:
            return {"ok": False, "reason": f"模块加载失败：{e}"}

        p = video_path
        if not p:
            with self._lock:
                outs = list(self._last_outputs)
            p = outs[0] if outs else ""
        if not p or not os.path.isfile(p):
            return {"ok": False, "reason": "找不到视频文件"}

        try:
            r = detect_subtitle_region(p)
        except Exception as e:
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

        if not r or not r.region:
            return {"ok": False, "reason": (r.reason if r else "未检测到")}

        # 顺带给出画面高度，便于界面显示相对位置
        h = 1080
        try:
            _w, h = FFmpeg().video_size(p)
        except Exception:
            pass
        out = r.to_dict()
        out["ok"] = True
        out["height"] = h
        return out

    def preview_desub(self, video_path: str = "", region: str = "",
                      method: str = "blur") -> Dict[str, Any]:
        """生成一张遮盖效果预览图（base64），供界面确认区域是否正确。

        校对场景里"看不见效果就不敢下手"，所以给个预览。
        """
        try:
            from .desub import Region, build_filter
        except Exception as e:
            return {"ok": False, "reason": str(e)}

        p = video_path
        if not p or not os.path.isfile(p):
            return {"ok": False, "reason": "找不到视频文件"}

        regions: List = []
        if region:
            nums = [v.strip() for v in region.replace("，", ",").split(",")]
            if len(nums) == 4:
                try:
                    regions = [Region(*(int(float(v)) for v in nums))]
                except Exception:
                    pass
        if not regions:
            rr = self.detect_subtitle(p)
            if rr.get("ok"):
                g = rr["region"]
                regions = [Region(g["x"], g["y"], g["w"], g["h"])]
        if not regions:
            return {"ok": False, "reason": "没有可用的遮盖区域"}

        try:
            ff = FFmpeg()
            w, h = ff.video_size(p)
            vf = build_filter(regions, method=method, frame_w=w, frame_h=h)
            dur = ff.duration(p) or 1
            t = min(max(1.0, dur * 0.3), 10.0)
            tmp = os.path.join(tempfile.gettempdir(), "subgen_desub_preview.jpg")
            r = subprocess.run(
                [ff.ffmpeg, "-hide_banner", "-v", "error", "-y",
                 "-ss", f"{t:.2f}", "-i", p, "-vf", vf,
                 "-frames:v", "1", "-q:v", "4", tmp],
                capture_output=True, timeout=90,
            )
            if r.returncode != 0 or not os.path.isfile(tmp):
                return {"ok": False, "reason": "预览生成失败"}
            import base64
            with open(tmp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"ok": True, "image": "data:image/jpeg;base64," + b64,
                    "region": regions[0].to_dict()}
        except Exception as e:
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    def probe(self, paths: List[str]) -> List[Dict[str, Any]]:
        """批量探测时长与体积，供列表展示。"""
        out = []
        try:
            ff = FFmpeg()
        except FFmpegNotFound:
            return out
        for p in (paths or []):
            try:
                d = ff.duration(p) if os.path.isfile(p) else 0.0
                sz = os.path.getsize(p) if os.path.isfile(p) else 0
                out.append({"path": p, "duration": round(d, 2), "size": sz})
            except Exception:
                out.append({"path": p, "duration": 0, "size": 0})
        return out

    def reveal(self, path: str) -> Dict[str, Any]:
        """在资源管理器里定位文件/打开目录。"""
        try:
            if not path:
                return {"ok": False, "error": "路径为空"}
            p = os.path.abspath(path)
            if os.path.isfile(p):
                # /select 会打开父目录并选中该文件
                subprocess.Popen(["explorer", "/select,", p])
                return {"ok": True}
            if os.path.isdir(p):
                os.startfile(p)  # noqa: S606
                return {"ok": True}
            d = os.path.dirname(p)
            if os.path.isdir(d):
                os.startfile(d)  # noqa: S606
                return {"ok": True}
            return {"ok": False, "error": "路径不存在"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------------------------------------------------------- 设置

    def load_settings(self) -> Dict[str, Any]:
        try:
            if os.path.isfile(SETTINGS_FILE):
                with open(SETTINGS_FILE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def save_settings(self, s: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s or {}, f, ensure_ascii=False, indent=2)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------------------------------------------------- 任务

    def start_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """启动后台处理。立即返回，进度通过 JS 回调推送。"""
        with self._lock:
            if self._job_thread and self._job_thread.is_alive():
                return {"ok": False, "error": "已有任务在运行"}
            paths = list((payload or {}).get("paths") or [])
            if not paths:
                return {"ok": False, "error": "没有待处理的文件"}
            s = dict((payload or {}).get("options") or {})
            self._cancel = threading.Event()
            self._last_outputs = []

            t = threading.Thread(
                target=self._run_job, args=(paths, s), daemon=True, name="subgen-job"
            )
            self._job_thread = t
            t.start()
            return {"ok": True}

    def cancel_job(self) -> Dict[str, Any]:
        self._cancel.set()
        rep = self._reporter
        if rep:
            rep.cancel()
        return {"ok": True}

    # ---- 内部：后台执行
    def _emit(self, js: str) -> None:
        """★ 这是本项目最容易踩的坑，展开说一下。

        问题：后台处理线程要把进度推给网页，但**不能直接碰 UI**。

        试过的两条路都不可靠：
          1. `window.evaluate_js()` —— 内部是 `Invoke(...)` + `semaphore.acquire()`，
             同步阻塞等结果。后台线程调用时 UI 线程正忙于消息循环，信号量等不到，
             处理线程被活活卡死。表现是：点开始后界面一直"待处理"，进度完全不动，
             而且不报任何错（异常还被吞掉了）。
          2. 直接调底层 WebView2 的 `ExecuteScriptAsync` —— COM 组件要求必须在
             UI 线程调用，从后台线程调会**静默失败**（不抛异常，但脚本没执行）。

        最终方案：**后台线程只往队列里塞事件，由前端定时来取**。
          - 写入端：本方法，只操作 deque（线程安全），完全不碰 UI
          - 读取端：poll_events()，由网页通过 pywebview 的 API 通道调用，
                    那时天然在 UI 线程上，安全

        这样跨线程问题从根上消失，而且顺带解决了"任务启动太快、前端还没准备好
        就收到了事件"的丢失问题——事件会缓存在队列里。
        """
        try:
            with self._ev_lock:
                self._events.append({"js": js})
        except Exception as e:
            self._log_error(f"事件入队失败: {type(e).__name__}: {e}")

    def poll_events(self) -> List[Dict[str, Any]]:
        """前端定时调用：取走累积的事件。

        返回 [{js: "..."}]，前端逐个 eval 执行。
        """
        with self._ev_lock:
            if not self._events:
                return []
            out = list(self._events)
            self._events.clear()
            return out

    def _log_error(self, msg: str) -> None:
        """把桥接层的错误写到文件（界面卡住时看不到任何输出）。"""
        try:
            with open(os.path.join(APP_DIR, "gui_error.log"), "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    @staticmethod
    def _get_native_view(w):
        """取底层 WebView2 控件（仅诊断用，正常流程不再依赖它）。"""
        try:
            from webview.platforms import winforms as wf
        except Exception:
            return None
        try:
            view = wf.BrowserView.instances.get(w.uid)
            return getattr(view, "webview", None) if view is not None else None
        except Exception:
            return None

    def _run_job(self, paths: List[str], s: Dict[str, Any]) -> None:
        _JOB_T0[0] = time.time()
        t0 = time.time()
        ok_count = fail_count = 0
        try:
            opts = self._build_options(paths, s)

            # 准备 ASR 模型（只下载/加载一次，批量复用）
            repo = resolve_model(opts.model)
            model_path = None
            try:
                if os.path.isdir(repo):
                    model_path = repo
                    self._emit(f"window.onLog && window.onLog('使用本地模型目录')")
                else:
                    cached = _model_cached(repo, opts.model_dir or MODEL_DIR)
                    if cached:
                        self._emit(f"window.onLog && window.onLog('识别模型已就绪，开始处理…')")
                    else:
                        # 先说清要下多大，再报实时进度 —— 大模型 2.9 GB，
                        # 不告诉用户要多久会让人以为卡死（踩过的坑）
                        size_hint = self._estimate_model_size(repo)
                        msg = (f"首次使用需下载识别模型（约 {size_hint}），"
                               f"已走国内镜像，请耐心等待…")
                        self._emit(f"window.onLog && window.onLog({json.dumps(msg)})")
                        self._emit(
                            "window.onDownloadStart && window.onDownloadStart("
                            + json.dumps({"repo": repo, "hint": size_hint}) + ")"
                        )

                    def _dl_progress(got, total, name, idx, cnt):
                        pct = (got / total * 100) if total else 0
                        self._emit(
                            "window.onDownloadProgress && window.onDownloadProgress("
                            + json.dumps({
                                "got": got, "total": total, "pct": round(pct, 1),
                                "name": name, "index": idx, "count": cnt,
                            }) + ")"
                        )

                    model_path = ensure_model(
                        repo, cache_dir=opts.model_dir or MODEL_DIR,
                        mirror=True, verbose=False,
                        on_progress=_dl_progress,
                        should_cancel=lambda: self._cancel.is_set(),
                    )
                    if not cached:
                        self._emit(f"window.onLog && window.onLog('识别模型下载完成')")
                        self._emit("window.onDownloadEnd && window.onDownloadEnd()")
            except Exception as e:
                msg = f"模型准备失败：{e}"
                self._emit(f"window.onFatal && window.onFatal({json.dumps(msg)})")
                return

            topts = self._transcribe_options(opts, repo)
            self._transcriber = Transcriber(topts, verbose=False, model_path=model_path)

            # 准备翻译器（可选）
            translator = None
            if opts.translate:
                try:
                    to = TranslateOptions(
                        src=norm_lang(opts.src_lang or opts.language or "zh") or "zh",
                        dst=norm_lang(opts.translate) or "en",
                        backend=opts.mt_backend,
                        model_dir=opts.mt_model_dir,
                        batch_size=opts.mt_batch,
                        glossary=opts.mt_glossary,
                        context=opts.mt_context,
                        api_base=opts.api_base,
                        api_key=opts.api_key,
                        llm_model=opts.llm_model,
                    )
                    translator = make_translator(to, verbose=False)
                    self._emit(
                        f"window.onLog && window.onLog('翻译引擎就绪（{opts.mt_backend}）')"
                    )
                except Exception as e:
                    msg = f"翻译器初始化失败：{e}"
                    self._emit(f"window.onLog && window.onLog({json.dumps(msg)})")
                    translator = None

            total = len(paths)
            for i, p in enumerate(paths, 1):
                if self._cancel.is_set():
                    break
                name = os.path.basename(p)

                # 让界面知道这是新文件（用于日志分组与显示时长/体积）
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                # 探测时长可能对大文件较慢，先发一条提示，避免界面静默
                self._emit(
                    f"window.onLog && window.onLog({json.dumps('分析文件信息…')})"
                )
                try:
                    d = FFmpeg().duration(p)
                except Exception:
                    d = 0.0
                self._emit(
                    "window.onFileStart && window.onFileStart("
                    + json.dumps({"name": name, "path": p, "size": sz,
                                  "duration": round(d, 2)}, ensure_ascii=False) + ")"
                )

                rep = CallbackReporter(
                    on_progress=lambda pr: self._emit(
                        f"window.onProgress && window.onProgress({json.dumps(self._p_json(pr), ensure_ascii=False)})"
                    ),
                    cancel_event=self._cancel,
                )
                self._reporter = rep
                rep.file_start(i, total, name)

                try:
                    res = run_one(
                        p, opts, reporter=rep,
                        transcriber=self._transcriber,
                        translator=translator,
                        model_path=model_path,
                    )
                except Cancelled:
                    rep.finish()
                    break
                except Exception as e:
                    res = None
                    err = f"{type(e).__name__}: {e}"
                    # 失败也要停心跳，否则这个文件的心跳会污染后面的日志和状态栏
                    rep.file_done(i, total, name, False, err)
                    self._emit(
                        f"window.onFileDone && window.onFileDone({json.dumps({'name': name, 'path': p, 'ok': False, 'error': err}, ensure_ascii=False)})"
                    )
                    fail_count += 1
                    continue

                if res and res.ok:
                    ok_count += 1
                    self._last_outputs.extend(res.outputs)
                else:
                    fail_count += 1

                # ★ 必须调用：它负责停掉心跳线程。
                # 漏掉会出现两个看似无关的现象：日志被同一句刷屏、
                # 状态栏被心跳覆盖导致永远显示"处理中"。
                rep.file_done(i, total, name, bool(res and res.ok),
                              (res.error if res else "未知错误"))

                payload = {
                    "name": name, "path": p,
                    "ok": bool(res and res.ok),
                    "error": (res.error if res else "未知错误"),
                    "outputs": (res.outputs if res else []),
                    "preview": (res.subtitle_preview if res else []),
                    "cues": (res.cues if res else 0),
                    "elapsed": (res.elapsed if res else 0),
                    "speed": (res.speed if res else 0),
                }
                self._emit(
                    f"window.onFileDone && window.onFileDone({json.dumps(payload, ensure_ascii=False)})"
                )

            elapsed = time.time() - t0
            done = {
                "ok_count": ok_count,
                "fail_count": fail_count,
                "elapsed": round(elapsed, 1),
                "has_preview": bool(ok_count),
                "cancelled": self._cancel.is_set(),
            }
            self._emit(
                f"window.onJobDone && window.onJobDone({json.dumps(done, ensure_ascii=False)})"
            )

        except Exception as e:
            self._emit(
                f"window.onFatal && window.onFatal({json.dumps(f'{type(e).__name__}: {e}')})"
            )
        finally:
            # 兜底停心跳：无论正常结束、异常还是取消，
            # 只要心跳还在跑，界面就会一直"处理中"、日志也会被刷屏。
            try:
                rep = self._reporter
                if rep is not None:
                    rep.finish()
            except Exception:
                pass
            self._reporter = None

    def _estimate_model_size(self, repo: str) -> str:
        """给出模型体积的人话描述。

        不要写死数字 —— large 是 2.9 GB、turbo 是 1.5 GB，
        早期把提示写死成"约 1.6 GB"，用户下 large 时看到这句会误判。
        """
        try:
            from .models import PRESETS
            for p in PRESETS.values():
                if p.repo == repo:
                    # vram_gb 是显存需求，磁盘体积约为其 0.6 倍再放宽些
                    if p.key == "large":
                        return "约 2.9 GB"
                    if p.key in ("turbo", "medium"):
                        return "约 1.5 GB"
                    if p.key == "small":
                        return "约 480 MB"
                    if p.key == "base":
                        return "约 145 MB"
                    return f"约 {p.vram_gb * 0.6:.1f} GB"
        except Exception:
            pass
        return "数百 MB 至数 GB"

    def _p_json(self, p: Progress) -> Dict[str, Any]:
        return {
            "stage": p.stage, "stage_label": p.stage_label,
            "current": round(p.current, 2), "total": round(p.total, 2),
            "file_index": p.file_index, "file_count": p.file_count,
            "file_name": p.file_name, "extra": p.extra,
            # elapsed 用真实时钟算，别用 p.elapsed 累加（心跳复用时容易错）
            "elapsed": round(max(0.0, time.time() - _JOB_T0[0]), 1) if _JOB_T0[0] else 0,
            "stage_elapsed": round(p.stage_elapsed, 1),
            "speed": round(p.speed, 2),
        }

    # ---- 参数转换
    def _build_options(self, paths: List[str], s: Dict[str, Any]) -> RunOptions:
        fmts = s.get("formats") or ["srt"]
        return RunOptions(
            inputs=paths,
            output=s.get("output") or "",
            formats=list(fmts),
            model=s.get("model") or "turbo",
            device=s.get("device") or "auto",
            compute_type=s.get("compute_type") or "auto",
            language=(s.get("language") or None),
            task="transcribe",
            prompt=(s.get("prompt") or None),
            vad=True,
            denoise=bool(s.get("denoise")),
            max_chars=int(s.get("max_chars") or 21),
            max_lines=2,
            max_duration=float(s.get("max_duration") or 7.0),
            # 识别结果后处理
            simplify=bool(s.get("simplify", True)),
            fixups=s.get("fixups") or "",
            translate=(s.get("translate") or None),
            src_lang=(s.get("language") or "zh"),
            bilingual=bool(s.get("bilingual")),
            mt_backend=s.get("backend") or "auto",
            mt_glossary=s.get("glossary") or "",
            api_key=s.get("api_key") or "",
            api_base=s.get("api_base") or "",
            desub=bool(s.get("desub")),
            desub_region=(s.get("desub_region") or ""),
            desub_method=(s.get("desub_method") or "blur"),
            desub_only=bool(s.get("desub_only")),
            burn=bool(s.get("burn")),
            mux=bool(s.get("mux")),
            model_dir=MODEL_DIR,
        )

    def _transcribe_options(self, opts: RunOptions, repo: str) -> TranscribeOptions:
        return TranscribeOptions(
            model=repo, device=opts.device, compute_type=opts.compute_type,
            language=opts.language, task=opts.task, beam_size=opts.beam_size,
            vad=opts.vad, initial_prompt=opts.prompt,
        )
