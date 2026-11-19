# 04 · 第二层：语音识别

> **本篇目标**：把 WAV 变成带时间戳的词流。理解设备探测与**精度选择**。
> **前置知识**：知道什么是 GPU 显存、什么是量化。
> **对应代码**：`subgen/engine.py`（264 行）+ `subgen/models.py`（405 行）
> **耗时**：40 分钟。

---

## 1. 本篇要解决的问题

拿到 16 kHz 单声道 WAV 之后，要把它变成**带时间戳的词**。

听起来就是「调个 API」？实际上有四个坑：

| 坑 | 现象 |
|---|---|
| CUDA DLL 找不到 | `Library cublas64_12.dll is not found` |
| 精度选错 | 慢到 0.3x 实时，用户以为卡死 |
| 模型下载 | 从 HuggingFace 拉 1.5 GB，国内经常超时 |
| 词太碎 | 出来的是「电 梯 里 年 轻 男 子」，没法用 |

前三个在本篇，第四个是 05 篇的主题。

---

## 2. 核心概念：int8 不是"降级"

这是全篇最重要的部分，也是最反直觉的。

### 实测数据

先看硬数据（GTX 1080 Ti，Pascal 架构 CC 6.1，34 秒中文音频）：

| 模型 | float32 | int8 | 加速比 |
|---|---|---|---|
| turbo | 32.4s（1.0x） | **2.9s（11.7x）** | 11× |
| large | 125.4s（0.3x） | **6.3s（5.4x）** | 20× |

**关键不只是快，还有：**

> **识别结果的 tokens 数完全一致**，质量损失极小。

### 那为什么直觉会觉得 int8 不行

因为「量化 = 降精度」这个印象。但语音识别对量化非常鲁棒：

- 输入本身就是 16 kHz 的音频，信息量远低于图像
- 模型的主要任务是模式匹配，不是精细数值计算
- int8 量化对权重的扰动，在 ASR 任务上的影响远小于人的预期

**结论：int8 在速度、显存、质量三个维度上全面占优，与显卡新旧无关。**

### 那 float16 呢——反直觉的地方

一般人会想：新卡支持 FP16，应该用 float16 最快吧？

**在 Pascal 架构（CC 6.1）上恰恰相反：**

```
float16 在 Pascal 消费卡上被限速到 FP32 的 1/64
而且指定它通常会直接报错
```

所以对 GTX 10 系（1080 Ti / 1070 / 1060 等）**必须避开 float16**。

### 我踩过的坑

第一次遇到这个问题时的思路是这样的：

> "float16 报错 → 那退回 float32 最稳妥"

结果：

```
large 模型 float32 → 0.3x 实时
```

40 分钟视频要跑 2 小时以上。用户看到进度条几乎不动，直接以为程序挂了。

**真正的正解不是"退回安全选项"，而是"换一个更合适的选项"。**

当时的思维盲区：只想着「在 float16 和 float32 之间选」，
完全没想到还有 int8 这条路。

### 代码里的表达

```python
# 计算精度选择（本机实测数据，GTX 1080 Ti / Pascal CC 6.1 / 34 秒音频）：
#   turbo float32  32.4s (1.0x)   |  turbo int8   2.5s (13.6x)
#   large float32 125.4s (0.3x)   |  large int8   7.0s ( 4.8x)
# 两条结论：
#   1. int8 比 float32 快 13~18 倍，而识别结果 tokens 数完全一致，
#      质量损失极小 —— 所以**一律优先 int8**，这与显卡新旧无关。
#   2. float16 在 Pascal（CC<7）消费卡上被限速到 FP32 的 1/64，
#      指定它会直接报错，必须避开。
cc = _detect_cc(out["names"])
if cc is not None and cc < 7.0:
    out["recommended_compute_type"] = "int8"
    out["note"] = "Pascal 架构：FP16 受限，int8 量化最快"
else:
    # 新卡上 int8 依然比 float16 快，且显存占用更低
    out["recommended_compute_type"] = "int8"
    out["note"] = "int8 量化，速度与显存均优于 float16"
```

注意两个分支**都返回 `int8`**。区别只是提示文案不同。

这看起来"if 白写了"，但保留了分支是为了**给用户解释为什么**——
用户看到"Pascal 架构：FP16 受限"，就不会怀疑程序选错了。

> 💡 **一个设计习惯**：当代码逻辑可以简化但**解释价值**还在时，
> 保留分支、把差异体现在**提示文案**上，而不是删掉。

---

## 3. 代码走读：CUDA DLL 注册

Windows 上最常见的报错：

```
Library cublas64_12.dll is not found or cannot be loaded
```

**原因**：`pip install nvidia-cublas-cu12` 把 DLL 放在
`site-packages/nvidia/cublas/bin/` 下，但这个目录**不在 Windows 的 DLL 搜索路径**里。

传统解法是让用户手动改 PATH——但这就违背了"装完就能用"。

```python
def _ensure_cuda_dlls() -> list[str]:
    """把 pip 安装的 NVIDIA CUDA 运行库目录加入 DLL 搜索路径。"""
    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return []
    root = list(spec.submodule_search_locations)[0]

    for pkg in sorted(os.listdir(root)):
        bindir = os.path.join(root, pkg, "bin")
        if not os.path.isdir(bindir):
            continue
        added.append(bindir)
        try:
            os.add_dll_directory(bindir)      # ← 关键 API
        except Exception:
            pass
        # 同时补进 PATH，供底层 native 加载器查找
        if bindir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    return added
```

**为什么两个都要做？**

| 方式 | 谁在用 |
|---|---|
| `os.add_dll_directory()` | Python 3.8+ 的标准机制，给 `ctypes` 用 |
| 改 `PATH` | **底层 native 加载器**（ctranslate2 是 C++ 写的） |

只做前者，某些情况下还是会失败。**两个都做才稳。**

> ⚠️ `os.add_dll_directory` 只在 Windows 和 Python 3.8+ 存在，
> 所以包在 `try/except` 里——其他平台静默跳过。

---

## 4. 代码走读：识别主流程

```python
class Transcriber:
    def __init__(self, opts: TranscribeOptions, *, verbose=True,
                 model_path: Optional[str] = None):
        self.opts = opts
        self.verbose = verbose
        self.model_path = model_path
        self._model = None

    def load(self) -> None:
        """把模型加载到显存。首次较慢（几秒到几十秒）。"""
        if self._model is not None:
            return
        added = _ensure_cuda_dlls()
        device, compute = _resolve_device(self.opts, self.verbose)

        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self.model_path or self.opts.model,
            device=device,
            compute_type=compute,
        )
```

**三个设计点**：

**① 懒加载**
`load()` 显式调用，不在 `__init__` 里加载。
因为加载要几秒，如果构造时加载，调用方很难在加载前显示"正在加载模型"。

**② 显存复用**
```python
if self._model is not None:
    return
```
批量处理 40 个文件时，模型只加载一次。**这是批量场景的关键优化**——
每次重载要 3~10 秒，40 个文件就是几分钟的纯浪费。

**③ CUDA DLL 注册在 load 之前**
必须在 `WhisperModel(...)` 之前调用，否则初始化时就崩了。

### `transcribe` 的核心

```python
segments, info = self._model.transcribe(
    audio,
    language=self.opts.language,          # None = 自动检测
    task=self.opts.task,                  # transcribe / translate
    beam_size=self.opts.beam_size,        # 默认 5
    vad_filter=self.opts.vad,             # 静音过滤
    initial_prompt=self.opts.initial_prompt,  # ★ 引导词
)
```

**`initial_prompt` 是个大杀器。**

Whisper 支持给一段"上文"来引导识别。对于短剧这种有大量专有名词的场景：

```bash
subgen.bat 视频.mp4 --prompt "角色：林晚晴、顾言深、苏蔓"
```

效果是**显著减少人名识别错误**。原理是这些词会进入模型的解码上下文，
让它在相似发音上倾向于选择你给的词。

> ⚠️ 但别塞太多。太长会挤占上下文，反而降低效果。经验值：**20~50 字**。

### `vad_filter` 要不要开

```python
vad_filter=self.opts.vad     # 默认 True
```

VAD（语音活动检测）会先过滤掉纯静音段，只把有人声的部分送进模型。

**好处**：长静音场景（如风景空镜）快很多，也不会在静音里"幻听"出内容。

**坏处**：如果音频质量差，VAD 可能误判，把人声也切掉。

默认开。出问题时第一个怀疑对象就是它（用 `--no-vad` 关掉对比）。

---

## 5. 模型下载：国内镜像

`models.py` 里 405 行，一大半是下载逻辑。核心问题：

**HuggingFace 在国内经常超时。**

```python
def ensure_model(repo, cache_dir, *, mirror=True, verbose=False,
                 on_progress=None, should_cancel=None) -> str:
    """确保模型在本地。缺什么下什么，支持断点续传。"""
```

三个关键设计：

**① 只下缺的文件**

不是"整个仓库打包下载"，而是先列文件清单，对比本地已有的，只补缺的。
模型仓库里通常有 `model.bin`（大头）+ 一堆小配置，断点续传很有用。

**② 按体积校验，不按时间**

```python
def _dir_matches(target: str, files: list[tuple[str, int]]) -> bool:
    """判断目录里的文件是否与期望的大小一致。"""
```

为什么不用修改时间？因为**复制、解压都会改时间戳**，但大小不会骗人。
用 (文件名, 字节数) 做校验，能准确判断"这个模型下全了没有"。

**③ `should_cancel` 回调**

下载 1.5 GB 要几分钟，用户可能想取消。
所以下载循环里定期检查：

```python
if should_cancel and should_cancel():
    raise Cancelled("用户取消下载")
```

---

## 6. 数据结构

```python
@dataclass
class TranscribeOptions:
    model: str = "turbo"
    device: str = "auto"            # auto / cuda / cpu
    compute_type: str = "auto"      # auto / float16 / float32 / int8
    language: Optional[str] = None  # None = 自动检测
    task: str = "transcribe"
    beam_size: int = 5
    vad: bool = True
    initial_prompt: Optional[str] = None
    cpu_fallback: bool = True       # 没 GPU 时降级到 CPU


@dataclass
class TranscribeResult:
    tokens: List[Token]
    language: str
    duration: float
    elapsed: float
    device: str
    compute_type: str

    @property
    def speed(self) -> float:
        """实时倍率。15.0 表示 15 倍速。"""
        return self.duration / self.elapsed if self.elapsed > 0 else 0.0
```

**`speed` 这个属性很有用**：界面上显示"15.1x 实时"，
用户立刻知道"40 分钟的片子大概 2.6 分钟跑完"。
没有这个数字，等待时间完全不可预期。

---

## 7. 动手练习

**练习 1：亲手验证 int8 的优势**

```bash
# int8（默认）
time .venv/Scripts/python.exe -m subgen.cli 测试.mp4 --compute-type int8 -o ./a

# float32
time .venv/Scripts/python.exe -m subgen.cli 测试.mp4 --compute-type float32 -o ./b
```

对比耗时，然后 `diff` 两个 srt。**你会发现内容几乎一样，但速度差十几倍。**

**练习 2：在 Pascal 卡上试 float16**

```bash
.venv/Scripts/python.exe -m subgen.cli 测试.mp4 --compute-type float16
```

看它报什么错。这个错你会很长记性。

**练习 3：观察 initial_prompt 的效果**

```bash
# 不带引导
subgen.bat 测试.mp4 -o ./no_prompt

# 带角色名引导
subgen.bat 测试.mp4 --prompt "角色：林晚晴、顾言深" -o ./with_prompt
```

diff 两个结果，找找人名部分。

**练习 4：关掉 VAD**

```bash
subgen.bat 测试.mp4 --no-vad -o ./no_vad
```

对比条数和内容。有背景音乐的场景差异会很明显。

---

## 8. 小结

**三句话**：

1. **int8 不是降级**，在速度/显存/质量上全面占优 —— 实测快 11~20 倍，tokens 数完全一致
2. **float16 在 Pascal 上必须避开**（被限速到 1/64 且通常报错），
   正确做法不是退回 float32，而是换 int8
3. CUDA DLL 要**同时**用 `os.add_dll_directory` 和改 `PATH` —— 只做一个不够稳

**下一篇** → [05 · 第三层：智能断句](05-segmentation.md) ⭐
**这是整个项目最核心的一篇**。ASR 给的词是碎的，怎么切成能看的字幕，全在这里。
