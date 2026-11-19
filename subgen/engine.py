"""faster-whisper 推理封装。

要点：
- 词级时间戳（word_timestamps）是精确断句的前提，默认开启
- VAD 过滤静音，既提速又能显著减少"幻觉"（音乐段凭空捏造文本）
- 长音频用 30s 滑窗天然支持，faster-whisper 内部已做分块
- 自动按显卡架构挑 compute_type，Pascal 卡退回 float32
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from .segmenter import Token


@dataclass
class TranscribeOptions:
    model: str = "turbo"
    device: str = "auto"            # auto | cuda | cpu
    compute_type: str = "auto"      # auto | float16 | float32 | int8 | int8_float16
    language: Optional[str] = None  # None = 自动检测；"zh" / "en" ...
    task: str = "transcribe"        # transcribe | translate（translate 会翻成英文）
    beam_size: int = 5
    vad: bool = True
    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 500
    word_timestamps: bool = True
    initial_prompt: Optional[str] = None
    condition_on_previous_text: bool = False
    temperature: float = 0.0
    no_speech_threshold: float = 0.6
    compression_ratio_threshold: float = 2.4
    threads: int = 4
    cpu_fallback: bool = True


@dataclass
class TranscribeResult:
    tokens: List[Token] = field(default_factory=list)
    segments: list = field(default_factory=list)
    language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0
    elapsed: float = 0.0
    device: str = ""
    compute_type: str = ""
    model: str = ""

    @property
    def speed(self) -> float:
        """实时倍率：音频时长 / 推理耗时。"""
        return self.duration / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def text(self) -> str:
        return "\n".join(s.text.strip() for s in self.segments if s.text.strip())


def _ensure_cuda_dlls() -> list[str]:
    """把 pip 安装的 NVIDIA CUDA 运行库目录加入 DLL 搜索路径。

    Windows 上 ctranslate2 需要 cublas64_12.dll / cudnn*.dll，但 pip 包
    （nvidia-cublas-cu12 等）把它们放在 site-packages/nvidia/*/bin 下，
    而该目录不在默认 DLL 搜索路径里，于是报
    "Library cublas64_12.dll is not found or cannot be loaded"。
    这里主动注册，免去用户手动配 PATH 的麻烦。
    """
    added: list[str] = []
    try:
        import importlib.util

        spec = importlib.util.find_spec("nvidia")
        if not spec or not spec.submodule_search_locations:
            return added
        root = list(spec.submodule_search_locations)[0]
    except Exception:
        return added

    if not os.path.isdir(root):
        return added

    for pkg in sorted(os.listdir(root)):
        bindir = os.path.join(root, pkg, "bin")
        if not os.path.isdir(bindir):
            continue
        added.append(bindir)
        try:
            os.add_dll_directory(bindir)  # Python 3.8+ on Windows
        except Exception:
            pass
        # 同时补进 PATH，供底层 native 加载器查找
        if bindir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    return added


def _resolve_device(opts: TranscribeOptions, verbose: bool) -> Tuple[str, str]:
    """决定 device 与 compute_type。"""
    device = opts.device
    compute = opts.compute_type

    from .models import gpu_summary

    info = gpu_summary()

    if device == "auto":
        device = "cuda" if info["count"] > 0 else "cpu"

    if compute == "auto":
        if device == "cuda":
            compute = info["recommended_compute_type"]
        else:
            compute = "int8"

    if device == "cuda" and info["count"] == 0 and opts.cpu_fallback:
        if verbose:
            print("[设备] 未检测到可用 CUDA，自动切换到 CPU")
        device, compute = "cpu", "int8"

    if verbose and device == "cuda" and info.get("cc") is not None and info["cc"] < 7.0:
        print(
            f"[设备] 检测到 Pascal 架构显卡（CC {info['cc']}）。"
            f"该架构 FP16 被限速、float32 又太慢，已选用 {compute}（实测快 13~18 倍）。"
        )

    return device, compute


class Transcriber:
    """包一层，模型只加载一次，支持多文件复用。"""

    def __init__(self, opts: TranscribeOptions, *, verbose: bool = True, model_path: Optional[str] = None):
        self.opts = opts
        self.verbose = verbose
        self._model = None
        self.device, self.compute_type = _resolve_device(opts, verbose)
        self.model_path = model_path

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "未安装 faster-whisper。请执行：pip install -r requirements.txt"
            ) from e

        src = self.model_path or self.opts.model
        if self.verbose:
            print(f"[模型] 加载 {src}  device={self.device}  compute_type={self.compute_type}")

        os.environ.setdefault("OMP_NUM_THREADS", str(max(1, self.opts.threads)))
        if self.device == "cuda":
            libs = _ensure_cuda_dlls()
            if self.verbose and libs:
                print(f"[设备] 已注册 {len(libs)} 个 CUDA 运行库路径")
        try:
            self._model = WhisperModel(
                src,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.opts.threads,
                num_workers=1,
            )
        except Exception as e:
            # 显存不足或 CUDA 库缺失时降级重试，避免直接崩掉
            if self.device == "cuda" and self.opts.cpu_fallback:
                if self.verbose:
                    print(f"[警告] GPU 加载失败（{type(e).__name__}: {e}），降级 CPU 重试")
                self.device, self.compute_type = "cpu", "int8"
                self._model = WhisperModel(
                    src, device="cpu", compute_type="int8", cpu_threads=self.opts.threads
                )
            else:
                raise

    def transcribe(
        self,
        audio_path: str,
        *,
        on_progress: Optional[Callable[[float, float], None]] = None,
        total_duration: float = 0.0,
    ) -> TranscribeResult:
        self.load()
        assert self._model is not None

        o = self.opts
        kwargs = dict(
            language=o.language,
            task=o.task,
            beam_size=o.beam_size,
            word_timestamps=o.word_timestamps,
            initial_prompt=o.initial_prompt,
            condition_on_previous_text=o.condition_on_previous_text,
            temperature=o.temperature,
            no_speech_threshold=o.no_speech_threshold,
            compression_ratio_threshold=o.compression_ratio_threshold,
            vad_filter=o.vad,
        )
        if o.vad:
            kwargs["vad_parameters"] = dict(
                threshold=o.vad_threshold,
                min_silence_duration_ms=o.vad_min_silence_ms,
            )

        t0 = time.time()
        segments, info = self._model.transcribe(audio_path, **kwargs)

        seg_list = []
        tokens: List[Token] = []
        for seg in segments:  # 生成器，迭代时真正开始推理
            seg_list.append(seg)
            if o.word_timestamps and getattr(seg, "words", None):
                for w in seg.words:
                    txt = (w.word or "").strip()
                    if not txt:
                        continue
                    tokens.append(
                        Token(
                            text=w.word,
                            start=float(w.start if w.start is not None else seg.start),
                            end=float(w.end if w.end is not None else seg.end),
                            prob=float(getattr(w, "probability", 1.0) or 1.0),
                        )
                    )
            else:
                # 无词级时间戳时退化为按句
                tokens.append(
                    Token(text=seg.text, start=float(seg.start), end=float(seg.end))
                )
            if on_progress and total_duration > 0:
                on_progress(min(seg.end, total_duration), total_duration)

        elapsed = time.time() - t0

        return TranscribeResult(
            tokens=tokens,
            segments=seg_list,
            language=getattr(info, "language", "") or "",
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            duration=float(getattr(info, "duration", 0.0) or total_duration),
            elapsed=elapsed,
            device=self.device,
            compute_type=self.compute_type,
            model=self.opts.model,
        )


def transcribe_file(
    audio_path: str,
    opts: TranscribeOptions,
    *,
    verbose: bool = True,
    model_path: Optional[str] = None,
    on_progress: Optional[Callable[[float, float], None]] = None,
    total_duration: float = 0.0,
) -> TranscribeResult:
    tr = Transcriber(opts, verbose=verbose, model_path=model_path)
    return tr.transcribe(audio_path, on_progress=on_progress, total_duration=total_duration)
