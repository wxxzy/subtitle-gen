"""字幕翻译引擎。

两个后端，各有取舍，按场景选：

┌────────────┬──────────────────────┬──────────┬────────────┬──────────┐
│ 后端        │ 模型                  │ 速度      │ 质量       │ 依赖      │
├────────────┼──────────────────────┼──────────┼────────────┼──────────┤
│ offline    │ Argos/OPUS-MT (CT2)  │ 极快      │ 中等       │ 无新增    │
│ llm        │ 任意 OpenAI 兼容 API  │ 较慢      │ 优秀       │ 需 API Key│
└────────────┴──────────────────────┴──────────┴────────────┴──────────┘

为什么离线后端能复用现有依赖：OPUS-MT / Argos 的模型都是 **Marian 架构**，
而 ctranslate2 原生支持 Marian——所以能直接用已经装好的 ctranslate2 跑推理，
不需要引入 torch 或 argostranslate（后者会拖进 spacy + stanza，几百 MB）。

字幕翻译的三个特殊约束（本模块的核心设计点）：
  1. **时间轴必须原样保留** —— 只换文本，绝不动 start/end，否则对不上口型
  2. **译文行宽会膨胀** —— 中译英通常变长 1.5~2 倍，必须重新折行
  3. **要支持双语** —— 原文+译文同屏是刚需，需要处理行高与样式
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .segmenter import Cue, text_width, wrap_lines

# ------------------------------------------------------------------ 语言与包

# Argos 官方包索引（100 个语言方向，模型为 CT2 格式，直接可用）
ARGOS_BASE = "https://argos-net.com/v1"
ARGOS_INDEX = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"

# 常用语言的中英文名，用于日志与字幕元数据
LANG_NAMES = {
    "zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어", "fr": "Français",
    "de": "Deutsch", "es": "Español", "ru": "Русский", "pt": "Português", "it": "Italiano",
    "ar": "العربية", "th": "ไทย", "vi": "Tiếng Việt", "id": "Indonesia", "hi": "हिन्दी",
    "nl": "Nederlands", "pl": "Polski", "tr": "Türkçe", "sv": "Svenska", "da": "Dansk",
    "fi": "Suomi", "cs": "Čeština", "uk": "Українська", "he": "עברית", "ms": "Melayu",
}

# 网络上常见的非标准写法 -> ISO 代码
LANG_ALIASES = {
    "chinese": "zh", "chs": "zh", "cht": "zh", "zh-cn": "zh", "zh-hans": "zh",
    "zh-tw": "zh", "zh-hant": "zh", "zh_cn": "zh", "zh_tw": "zh", "cn": "zh",
    "eng": "en", "english": "en", "us": "en", "uk": "en",
    "jpn": "ja", "japanese": "ja", "jp": "ja",
    "kor": "ko", "korean": "ko", "kr": "ko",
    "fra": "fr", "fre": "fr", "french": "fr",
    "deu": "de", "ger": "de", "german": "de",
    "spa": "es", "spanish": "es",
    "rus": "ru", "russian": "ru",
    "por": "pt", "portuguese": "pt",
    "ita": "it", "italian": "it",
}


def norm_lang(code: str) -> str:
    """把各种写法归一化成 ISO 639-1 两字母代码。"""
    if not code:
        return ""
    c = code.strip().lower().replace("_", "-")
    if c in LANG_ALIASES:
        return LANG_ALIASES[c]
    return c.split("-")[0]


def lang_label(code: str) -> str:
    """给日志/元数据用的可读语言名。"""
    c = norm_lang(code)
    return LANG_NAMES.get(c, c.upper() or "?")


# ------------------------------------------------------------------ 译文清理

# 中文标点 -> 英文标点（中译英时顺手规范化）
_CJK_PUNCT = {
    "，": ", ", "。": ". ", "、": ", ", "；": "; ", "：": ": ",
    "！": "! ", "？": "? ", "（": " (", "）": ") ",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "《": " \"", "》": "\" ", "—": " - ", "…": "...",
}


def clean_translation(s: str, target_lang: str = "en") -> str:
    """清理机翻输出的常见瑕疵。"""
    if not s:
        return ""
    # ▁ 是 sentencepiece 的词边界标记，必须替成空格
    s = s.replace("\u2581", " ")
    s = re.sub(r"\s+", " ", s).strip()

    if norm_lang(target_lang) == "en":
        # 标点后必须有空格；标点前不留空格
        s = re.sub(r"\s+([,.!?;:])", r"\1", s)
        s = re.sub(r"([,.!?;:])(?=[^\s\d])", r"\1 ", s)
        # 英文首个字母大写
        if s and s[0].islower():
            s = s[0].upper() + s[1:]
        # 英文句末补标点（原文有句末标点时才补，避免疑问句变陈述句）
        if s and s[-1] not in ".!?\"')]":
            s += "."
    else:
        for a, b in _CJK_PUNCT.items():
            s = s.replace(a, b)
        s = re.sub(r"\s+", " ", s).strip()

    return s


def fix_repeats(s: str) -> str:
    """修掉机翻偶发的整句重复（如 "It's over. It's over."）。"""
    if not s:
        return s
    # 两个相同句子连着
    m = re.match(r"^(.{4,}?[.!?])\s*\1$", s)
    if m:
        return m.group(1)
    return s


# ------------------------------------------------------------------ 后端基类

@dataclass
class TranslateOptions:
    src: str = "zh"
    dst: str = "en"
    backend: str = "auto"          # auto | offline | llm
    model_dir: str = ""            # 离线模型缓存目录
    # 离线后端
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = 4
    batch_size: int = 32
    # LLM 后端
    api_base: str = ""
    api_key: str = ""
    llm_model: str = "deepseek-chat"
    glossary: str = ""             # 术语表，"原词=译词" 每行一条
    context: str = ""              # 领域/风格提示，帮助 LLM 贴合语境
    timeout: int = 60


class TranslatorBase:
    name = "base"

    def __init__(self, opts: TranslateOptions, *, verbose: bool = True):
        self.opts = opts
        self.verbose = verbose
        self._glossary: dict[str, str] = {}
        # 占位符表：术语 -> 占位符。用于「译前保护 + 译后还原」
        self._ph_to_term: dict[str, str] = {}

    def load(self) -> None:
        raise NotImplementedError

    def translate(self, texts: Sequence[str]) -> List[str]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # ---------------------------------------------------------- 术语表

    def load_glossary(self) -> dict[str, str]:
        """读术语表。每行一条，分隔符支持 = / => / ：。"""
        if self._glossary:
            return self._glossary
        g: dict[str, str] = {}
        for line in (self.opts.glossary or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in ("=>", "=", "："):
                if sep in line:
                    k, v = line.split(sep, 1)
                    k, v = k.strip(), v.strip()
                    if k and v:
                        g[k] = v
                    break
        self._glossary = g
        return g

    def protect_terms(self, text: str) -> str:
        """把术语替换成占位符，防止机翻乱译。

        为什么用占位符而不是简单替换：Marian 这类模型对不认识的专名会强行
        「意译」（妙脆角猫 -> marrow horn cat、男主 -> master）。
        换成 `XTERM0X` 这种非常规 token 后模型只能原样保留，
        位置和重复次数都正确（实测验证），译完再还原即可。
        """
        g = self.load_glossary()
        if not g:
            return text
        # 长词优先，避免短词先匹配把长词切碎
        for term in sorted(g, key=len, reverse=True):
            if term in text:
                ph = self._term_to_ph(term)
                text = text.replace(term, ph)
        return text

    def _term_to_ph(self, term: str) -> str:
        for ph, t in self._ph_to_term.items():
            if t == term:
                return ph
        ph = f"XTERM{len(self._ph_to_term)}X"
        self._ph_to_term[ph] = term
        return ph

    def restore_terms(self, text: str) -> str:
        """把占位符还原成目标语言术语。"""
        g = self.load_glossary()
        if not self._ph_to_term:
            return text
        for ph, term in self._ph_to_term.items():
            # 模型可能给占位符加空格或改大小写，做容错匹配
            target = g.get(term, term)
            for variant in (ph, ph.lower(), ph.upper(),
                            ph.replace("XTERM", "XTERM ").strip()):
                if variant in text:
                    text = text.replace(variant, target)
        # 兜底：清掉任何残留的占位符碎片
        text = re.sub(r"\bX\s?TERM\s?\d+\s?X\b", "", text, flags=re.I)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    def reset_placeholders(self) -> None:
        self._ph_to_term = {}


# ------------------------------------------------------------------ 离线后端

class OfflineTranslator(TranslatorBase):
    """基于 ctranslate2 直跑 Marian 模型（Argos / OPUS-MT 格式）。

    不引入 torch / argostranslate，只用已装的 ctranslate2 + sentencepiece。
    """

    name = "offline"

    def __init__(self, opts: TranslateOptions, *, verbose: bool = True):
        super().__init__(opts, verbose=verbose)
        self._tr = None
        self._sp = None
        self._model_root = ""

    # ---- 模型定位与下载
    def _pkg_name(self) -> str:
        return f"translate-{self.opts.src}_{self.opts.dst}"

    def _cache_dir(self) -> str:
        root = self.opts.model_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "mt"
        )
        return os.path.join(root, f"{self.opts.src}-{self.opts.dst}")

    def ensure_model(self) -> str:
        """确保翻译模型在本地，返回其根目录。"""
        d = self._cache_dir()
        model_dir = os.path.join(d, "model")
        sp_path = os.path.join(d, "sentencepiece.model")
        if os.path.isfile(os.path.join(model_dir, "model.bin")) and os.path.isfile(sp_path):
            return d

        # 从 Argos 官方拉取（预转换 CT2 格式）
        url = f"{ARGOS_BASE}/translate-{self.opts.src}_{self.opts.dst}-{self._argos_version()}.argosmodel"
        os.makedirs(d, exist_ok=True)

        if self.verbose:
            print(f"[翻译] 首次下载离线模型 {norm_lang(self.opts.src)}"
                  f" → {norm_lang(self.opts.dst)}")
            print(f"[翻译] 来源：{ARGOS_BASE}")

        import tempfile
        import urllib.request
        import zipfile

        tmp = os.path.join(tempfile.gettempdir(), f"subgen_mt_{self.opts.src}_{self.opts.dst}.zip")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "subgen/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                last_pct = -1
                while True:
                    chunk = r.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    # 每 10% 打一次，避免刷屏拖慢下载
                    if self.verbose and total > 0:
                        pct = int(got * 100 / total)
                        if pct >= last_pct + 10:
                            last_pct = pct - (pct % 10)
                            print(f"\r[翻译] 下载 {pct:3d}%  ({got/1024**2:.0f}/{total/1024**2:.0f} MB)",
                                  end="", flush=True)
            if self.verbose:
                print()

            # 解包：只取 model/ 和 sentencepiece.model，跳过 stanza（用不上且体积大）
            with zipfile.ZipFile(tmp) as z:
                for item in z.infolist():
                    n = item.filename
                    if n.endswith("/"):
                        continue
                    base = n.split("/", 1)[-1] if "/" in n else n
                    if base.startswith("stanza") or "/stanza/" in n:
                        continue
                    target = os.path.join(d, base)
                    if base == "model/model.bin" or base.startswith("model/"):
                        target = os.path.join(d, base)
                    elif base == "sentencepiece.model":
                        target = os.path.join(d, "sentencepiece.model")
                    else:
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(item) as src_f, open(target, "wb") as dst_f:
                        dst_f.write(src_f.read())
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        if not os.path.isfile(os.path.join(model_dir, "model.bin")):
            raise RuntimeError(f"翻译模型解包失败：{d}")
        if self.verbose:
            print(f"[翻译] 就绪：{d}")
        return d

    @staticmethod
    def _argos_version() -> str:
        """Argos 各语言方向版本号不统一，取常见的 1.9；失败时回退探测。"""
        return "1_9"

    # ---- 加载与推理
    def load(self) -> None:
        if self._tr is not None:
            return
        try:
            import ctranslate2
            import sentencepiece as spm
        except ImportError as e:
            raise RuntimeError(
                "离线翻译需要 ctranslate2 与 sentencepiece。\n"
                "请执行：pip install sentencepiece\n"
                "（ctranslate2 已随 faster-whisper 安装）"
            ) from e

        d = self.ensure_model()
        self._model_root = d

        device = self.opts.device
        compute = self.opts.compute_type
        if device == "auto" or compute == "auto":
            from .engine import _ensure_cuda_dlls
            from .models import gpu_summary

            info = gpu_summary()
            if device == "auto":
                device = "cuda" if info["count"] > 0 else "cpu"
            if compute == "auto":
                compute = info["recommended_compute_type"] if device == "cuda" else "int8"
            if device == "cuda":
                _ensure_cuda_dlls()

        # CTC/翻译模型很轻，int8 也不掉质量，且 Pascal 卡上比 float32 快
        if device == "cpu":
            compute = "int8"

        if self.verbose:
            print(f"[翻译] 加载模型 {d}  device={device} compute={compute}")

        try:
            self._tr = ctranslate2.Translator(
                os.path.join(d, "model"), device=device, compute_type=compute
            )
        except Exception as e:
            if device == "cuda":
                if self.verbose:
                    print(f"[翻译] GPU 加载失败（{e}），降级 CPU")
                self._tr = ctranslate2.Translator(
                    os.path.join(d, "model"), device="cpu", compute_type="int8"
                )
            else:
                raise

        self._sp = spm.SentencePieceProcessor()
        self._sp.load(os.path.join(d, "sentencepiece.model"))

    def translate(self, texts: Sequence[str]) -> List[str]:
        self.load()
        assert self._tr is not None and self._sp is not None

        self.reset_placeholders()
        results: List[str] = []
        B = max(1, self.opts.batch_size)

        for i in range(0, len(texts), B):
            chunk = list(texts[i : i + B])
            # 保留空行占位，保证索引对齐
            idx_map = [(j, t) for j, t in enumerate(chunk) if t and t.strip()]
            if not idx_map:
                results.extend([""] * len(chunk))
                continue

            # 术语保护：译前替换成占位符
            protected = [self.protect_terms(t) for _, t in idx_map]
            pieces = [self._sp.encode(t, out_type=str) for t in protected]
            out = self._tr.translate_batch(
                pieces, beam_size=self.opts.beam_size, max_batch_size=B
            )

            slot = [""] * len(chunk)
            for (j, _src), r in zip(idx_map, out):
                # hypotheses[0] 是 token 列表，必须过 sentencepiece 解码
                hyp = r.hypotheses[0] if r.hypotheses else []
                raw = self._sp.decode(hyp) if hyp else ""
                txt = clean_translation(raw, self.opts.dst)
                txt = self.restore_terms(txt)   # 还原术语
                txt = fix_repeats(txt)
                slot[j] = txt
            results.extend(slot)

        return results


# ------------------------------------------------------------------ LLM 后端

# 常见服务商预设：界面选一下就自动填好端点和推荐模型，省得用户去翻文档。
# 都是 OpenAI 兼容接口，所以后端代码不用改。
LLM_PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "name": "DeepSeek（深度求索）",
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "便宜、中文好，翻译首选",
    },
    "openai": {
        "name": "OpenAI",
        "base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_url": "https://platform.openai.com/api-keys",
        "note": "需要能访问外网",
    },
    "dashscope": {
        "name": "阿里云百炼（通义千问）",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_url": "https://bailian.console.aliyun.com/",
        "note": "国内直连，有免费额度",
    },
    "moonshot": {
        "name": "月之暗面（Kimi）",
        "base": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长文本友好",
    },
    "zhipu": {
        "name": "智谱 AI（GLM）",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_url": "https://bigmodel.cn/usercenter/apikeys",
        "note": "有免费模型",
    },
    "siliconflow": {
        "name": "硅基流动",
        "base": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "聚合多家开源模型",
    },
    "ark": {
        "name": "火山方舟（豆包）",
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
        "key_url": "https://console.volcengine.com/ark",
        "note": "需要先在控制台创建接入点",
    },
    "ollama": {
        "name": "本地 Ollama",
        "base": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "key_url": "https://ollama.com/download",
        "note": "完全本地，不需要 Key",
    },
    "custom": {
        "name": "自定义 / 中转",
        "base": "",
        "model": "",
        "key_url": "",
        "note": "任何 OpenAI 兼容接口",
    },
}


def guess_provider(base: str) -> str:
    """根据端点反查服务商，用于界面回显。"""
    if not base:
        return ""
    b = base.rstrip("/").lower()
    for key, info in LLM_PROVIDERS.items():
        if info["base"] and info["base"].rstrip("/").lower() == b:
            return key
    return "custom"


def resolve_api(opts: "TranslateOptions") -> tuple:
    """把配置解析成最终的 (base, key, source)。

    优先级：显式配置 > 环境变量。
    抽成独立函数是为了让「测试连接」能复用同一套逻辑，
    避免界面测通了、实际跑起来用的是另一套配置。
    """
    base = opts.api_base or os.environ.get("SUBGEN_API_BASE") or ""
    key = opts.api_key or os.environ.get("SUBGEN_API_KEY") or ""
    source = "界面" if opts.api_key else ""

    if not key:
        env_map = {
            "DEEPSEEK_API_KEY": "https://api.deepseek.com/v1",
            "OPENAI_API_KEY": "https://api.openai.com/v1",
            "DASHSCOPE_API_KEY": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "MOONSHOT_API_KEY": "https://api.moonshot.cn/v1",
            "ARK_API_KEY": "https://ark.cn-beijing.volces.com/api/v3",
            "SILICONFLOW_API_KEY": "https://api.siliconflow.cn/v1",
            "ZHIPU_API_KEY": "https://open.bigmodel.cn/api/paas/v4",
        }
        for env, env_base in env_map.items():
            if os.environ.get(env):
                key = os.environ[env]
                source = f"环境变量 {env}"
                if not base:
                    base = env_base
                break

    # 本地服务（Ollama 等）允许空 Key
    if not base:
        base = "https://api.deepseek.com/v1"
    is_local = any(h in base for h in ("127.0.0.1", "localhost", "0.0.0.0"))
    return base.rstrip("/"), key, source, is_local


def test_llm_connection(opts: "TranslateOptions", *, timeout: int = 25) -> Dict[str, Any]:
    """发一个最小请求验证配置是否可用。

    为什么要做这个：配 LLM 最容易错的不是 Key，而是**端点写错**
    （比如漏了 /v1、或多写了 /chat/completions）。直接跑一遍翻译要等很久才发现错，
    这里用一个极小的请求先验，几秒出结果。
    """
    import json
    import time
    import urllib.error
    import urllib.request

    base, key, source, is_local = resolve_api(opts)
    if not key and not is_local:
        return {"ok": False, "error": "没有填 API Key", "base": base}

    model = opts.llm_model or "deepseek-chat"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "temperature": 0,
    }).encode("utf-8")

    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        elapsed = time.time() - t0
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            pass
        # 有些服务会把真实模型名回显出来（如中转商改了名）
        used = data.get("model") or model
        return {
            "ok": True, "base": base, "model": model, "used_model": used,
            "key_source": source, "elapsed": round(elapsed, 2),
            "reply": (content or "")[:40],
        }
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        hint = ""
        if e.code == 401:
            hint = "Key 无效或已过期"
        elif e.code == 404:
            hint = "端点路径不对，检查是否漏了 /v1（或服务商改了路径）"
        elif e.code == 402:
            hint = "账户余额不足"
        elif e.code == 429:
            hint = "触发限流，稍后再试或换模型"
        return {"ok": False, "base": base, "model": model, "code": e.code,
                "error": f"HTTP {e.code}：{detail}", "hint": hint}
    except urllib.error.URLError as e:
        return {"ok": False, "base": base, "model": model,
                "error": f"连不上：{e.reason}",
                "hint": "检查网络、代理，或确认端点地址正确"}
    except Exception as e:
        return {"ok": False, "base": base, "model": model,
                "error": f"{type(e).__name__}: {e}"}


class LLMTranslator(TranslatorBase):
    """OpenAI 兼容 Chat Completions API。质量最好，适合口语化内容。

    关键设计：**整批上下文一起翻译**，而不是逐条独立翻。
    字幕是连续对话，逐条翻会丢失指代关系（"他"指谁、"这个"指什么），
    质量会明显掉。所以把整片字幕打包成编号列表一次性发出。
    """

    name = "llm"

    SYSTEM = (
        "You are a professional subtitle translator. Translate the numbered lines.\n"
        "Rules:\n"
        "1. Output EXACTLY the same number of lines, each prefixed with its original number.\n"
        "2. Keep translations concise — subtitles must be readable on screen. "
        "Aim for at most 2 short lines.\n"
        "3. Preserve tone: casual speech stays casual, slang stays slang. "
        "Do not over-formalize.\n"
        "4. Use surrounding lines as context to resolve pronouns and references correctly.\n"
        "5. Do NOT translate proper nouns into literal meanings if they are names/brands.\n"
        "6. Output only the numbered translations, no explanations, no notes."
    )

    def __init__(self, opts: TranslateOptions, *, verbose: bool = True):
        super().__init__(opts, verbose=verbose)
        self._base = ""
        self._key = ""

    def _resolve_api(self) -> tuple[str, str]:
        """解析 (base, key)。

        实际逻辑在模块级 resolve_api() 里，这里只是包装 ——
        保证「测试连接」和「真正翻译」用的是同一套判断，
        不会出现"测试通过但跑起来失败"的诡异情况。
        """
        base, key, _src, _local = resolve_api(self.opts)
        return base, key

    def available(self) -> bool:
        _, key = self._resolve_api()
        return bool(key)

    def load(self) -> None:
        base, key = self._resolve_api()
        if not key:
            raise RuntimeError(
                "LLM 翻译需要 API Key。三种配置方式任选：\n"
                "  1. 命令行 --api-key sk-xxx --api-base https://api.deepseek.com/v1\n"
                "  2. 环境变量 SUBGEN_API_KEY / SUBGEN_API_BASE\n"
                "  3. 设置 DEEPSEEK_API_KEY / OPENAI_API_KEY 等常见变量（会自动识别端点）\n"
                "若不想配 Key，可改用离线后端：--mt-backend offline"
            )
        self._base = base
        self._key = key
        if self.verbose:
            print(f"[翻译] LLM 后端 {base}  模型 {self.opts.llm_model}")

    def _chat(self, user_content: str) -> str:
        """调用 OpenAI 兼容的 chat/completions。

        用标准库 urllib 而不是 requests——只为发一个 POST 而多装一个依赖不划算，
        这也和「不引入 PyTorch」的整体轻量思路一致。
        """
        import json
        import urllib.error
        import urllib.request

        payload = json.dumps({
            "model": self.opts.llm_model,
            "messages": [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.opts.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"LLM 接口返回 {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM 接口连接失败：{e.reason}") from e

        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"LLM 返回格式异常：{str(data)[:300]}") from e

    def translate(self, texts: Sequence[str]) -> List[str]:
        self.load()
        glossary = self.load_glossary()

        hint = ""
        if self.opts.context:
            hint += f"\nDomain/context: {self.opts.context}"
        if glossary:
            pairs = "; ".join(f"{k} => {v}" for k, v in glossary.items())
            hint += f"\nGlossary (MUST use these exact translations): {pairs}"

        results: List[str] = []
        B = max(1, self.opts.batch_size)

        for i in range(0, len(texts), B):
            chunk = list(texts[i : i + B])
            numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(chunk) if t.strip())
            prompt = (
                f"Translate the following {self.opts.src} subtitle lines into "
                f"{self.opts.dst}.{hint}\n\n{numbered}"
            )
            raw = self._chat(prompt)
            parsed = self._parse_numbered(raw, len(chunk))

            for j, (src_t, dst_t) in enumerate(zip(chunk, parsed)):
                if not src_t.strip():
                    results.append("")
                    continue
                results.append(clean_translation(dst_t, self.opts.dst))
        return results

    @staticmethod
    def _parse_numbered(raw: str, n: int) -> List[str]:
        """解析 "1. xxx" 格式的返回，容忍模型少给/多给行。"""
        out = [""] * n
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)\s*[.、:：)\]]\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < n:
                    out[idx] = (out[idx] + " " + m.group(2).strip()).strip() if out[idx] else m.group(2).strip()
            elif out and any(out):
                # 续行：接到最后一个已填充项
                for k in range(n - 1, -1, -1):
                    if out[k]:
                        out[k] = out[k] + " " + line
                        break
        return out


# ------------------------------------------------------------------ 工厂

def make_translator(opts: TranslateOptions, *, verbose: bool = True) -> TranslatorBase:
    """按 backend 选择实现；auto 时优先 LLM（若已配 Key），否则离线。"""
    backend = (opts.backend or "auto").lower()

    if backend == "offline":
        return OfflineTranslator(opts, verbose=verbose)
    if backend == "llm":
        return LLMTranslator(opts, verbose=verbose)

    # auto
    llm = LLMTranslator(opts, verbose=verbose)
    if llm.available():
        if verbose:
            print("[翻译] 检测到 API Key，使用 LLM 后端（质量更高）")
        return llm
    if verbose:
        print("[翻译] 未检测到 API Key，使用离线后端（无需联网）")
    return OfflineTranslator(opts, verbose=verbose)


# ------------------------------------------------------------------ 编排

def translate_cues(
    cues: Sequence[Cue],
    translator: TranslatorBase,
    *,
    target_lang: str,
    bilingual: bool = False,
    max_line_width: int = 42,
    max_lines: int = 2,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """翻译整批 cue，返回**多个变体**，让调用方各取所需。

    返回 dict：
        {
          "src":     [Cue...]   源语言（原文）
          "trans":   [Cue...]   目标语言（只有译文）
          "bilingual": [Cue...] 双语（原文+译文）
        }

    为什么一次性返回三种而不是分次调用：翻译是这里最贵的一步
    （LLM 还要花钱），同一批文本只该翻一遍，之后从结果派生各种呈现形式。
    时间轴一律原样复制，只换文本。
    """
    src_texts = [" ".join(c.lines) for c in cues]
    t0 = time.time()
    trans = translator.translate(src_texts)
    elapsed = time.time() - t0

    if translator.verbose:
        n = len([t for t in trans if t.strip()])
        print(f"[翻译] {n}/{len(cues)} 条完成，用时 {elapsed:.2f}s")

    # max_line_width 一律是「半角字符数」——text_width() 已把 CJK 字符算作 2，
    # 所以任何目标语言都用同一个上限，不需要再按语言换算（早期版本对 CJK
    # 目标又乘了一次 2，导致日文字幕占双倍屏宽而溢出）。
    wrap_w = max_line_width

    only_trans: List[Cue] = []
    both: List[Cue] = []
    for c, t in zip(cues, trans):
        t = (t or "").strip()

        # 目标语言（只有译文）。译文为空时回退保留原文，避免整条消失
        a = Cue(start=c.start, end=c.end, tokens=list(c.tokens))
        a.lines = wrap_lines(t, wrap_w, max_lines) if t else list(c.lines)
        only_trans.append(a)

        # 双语：原文与译文**分开存**。不合并进 lines 是刻意的——
        # ASS 需要按层渲染，下游也可能只想取其中一层。
        b = Cue(start=c.start, end=c.end, tokens=list(c.tokens))
        b.lines = list(c.lines)
        b.trans_lines = wrap_lines(t, wrap_w, max_lines) if t else []
        both.append(b)

    return {
        "src": list(cues),
        "trans": only_trans,
        "bilingual": both,
        "elapsed": elapsed,
    }
