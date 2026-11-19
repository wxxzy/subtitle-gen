# 02 · 整体架构与数据流

> **本篇目标**：脑中有全局图。知道每一层管什么、层与层之间传什么、为什么这么切。
> **前置知识**：01 篇跑通过。
> **耗时**：30 分钟。

---

## 1. 先看全景

整个项目按「一件事只在一个地方做」的原则切成六层：

```
                    ┌─────────────────────────────────┐
   用户入口          │  cli.py (命令行) │ gui.py (界面)  │
                    └────────┬─────────────────────────┘
                             │  ① 用户意图（RunOptions）
                    ┌────────▼─────────────────────────┐
   编排层            │        tasks.py / run_one        │
                    │   串流程、处理容错、汇总结果        │
                    └────────┬─────────────────────────┘
                             │  ② 按顺序调用下层
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   ┌────▼────┐      ┌────────▼───────┐    ┌───────▼──────┐
   │ audio   │      │ engine+models  │    │ segmenter    │
   │ 音视频   │      │ 语音识别        │    │ 智能断句      │
   └────┬────┘      └────────┬───────┘    └───────┬──────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │  ③ 产物
                    ┌────────▼─────────────────────────┐
   输出层            │  formats │ translator │ desub   │
                    │  字幕格式 │ 翻译       │ 去字幕   │
                    └────────┬─────────────────────────┘
                             │
                    ┌────────▼─────────────────────────┐
   横向支撑          │  reporter.py  (进度/取消)         │
                    │  models.py    (模型/设备)         │
                    └──────────────────────────────────┘
```

**两条关键规则**：

1. **上层可以调下层，下层不知道上层存在**
   `segmenter.py` 完全不知道有 CLI 和 GUI 这回事。
2. **横向支撑模块被所有层使用**
   `reporter` 和 `models` 是"公共设施"，不参与业务流。

---

## 2. 数据流的六个阶段

这是核心。记住这六个转换，整个项目就理解了 70%：

| 阶段 | 输入 | 输出 | 在哪 |
|---|---|---|---|
| ① 抽音频 | `.mp4` / `.mkv` | 16 kHz 单声道 WAV | `audio.py` |
| ② 识别 | WAV | `List[Token]`（带时间戳的词） | `engine.py` |
| ③ 断句 | `List[Token]` | `List[Cue]`（字幕行） | `segmenter.py` |
| ④ 翻译 | `List[Cue]` | `List[Cue]`（带译文） | `translator.py` |
| ⑤ 输出 | `List[Cue]` | `.srt` / `.vtt` / ... | `formats.py` |
| ⑥ 合成 | 视频 + 字幕 | 成品视频 | `audio.py` |

### 最关键的两次「数据结构变形」

```
① 文件  →  波形
   音频是连续信号，采样率 44100 Hz、双声道、16 bit

② 波形  →  Token 列表
   [Token(text="电梯里", start=0.0, end=0.8),
    Token(text="年轻",   start=0.8, end=1.2),
    Token(text="男子",   start=1.2, end=1.5), ...]
   ⚠️ 这一刻词是「碎」的，直接上屏完全没法看

③ Token →  Cue 列表          ← ★ 本项目的核心价值
   [Cue(lines=["电梯里年轻男子"], start=0.0, end=2.6),
    Cue(lines=["带双马尾的女友回家"], start=2.6, end=5.1), ...]
   ✅ 这才是能上屏的字幕
```

**②→③ 这一步是分水岭。** 很多人以为「Whisper 出字幕」就是直接调 API，
其实 ASR 只给你词，**怎么切行、怎么控制时长、怎么对齐标点**全是自己做的。

05 篇会用一整篇讲这个。

---

## 3. 分层是怎么切出来的

### 判断标准：能不能单独测试

一个好的分层，每一层都能**脱离其他层单独测**。

```python
# segmenter 可以完全不知道 ffmpeg 和 whisper 的存在
tokens = [Token("你好", 0.0, 1.0), Token("世界", 1.0, 2.0)]
cues = build_cues(tokens, max_line_width=42)
assert cues[0].text == "你好世界"
```

能做到这一点，是因为层间只通过**简单的数据结构**通信：
- `audio.py` 只产出**文件路径**
- `engine.py` 只产出 `List[Token]`
- `segmenter.py` 只吃 `List[Token]`、吐 `List[Cue]`
- `formats.py` 只吃 `List[Cue]`

**没有任何一层传递「对象引用」或「全局状态」。**

> 💡 这是可测试性的基础。如果你发现自己写的一层没法单独测，
> 八成是它偷偷依赖了别的东西。

### 为什么 `reporter` 是横向的

进度上报不属于任何一层——音视频层要报、识别层要报、翻译层也要报。
如果塞进某一层，其他层就用不了。

所以它被设计成**接口**（`Reporter`），由调用方注入：

```python
def run_one(src, opts, *, reporter: Optional[Reporter] = None):
    rep = reporter or NullReporter()   # ← 不传就用空实现
    rep.stage("audio", "抽取音轨…")
```

**这个模式叫「依赖注入」**，好处是：
- CLI 用 `CliReporter` 打印进度条
- GUI 用 `CallbackReporter` 推给网页
- 测试用 `NullReporter` 什么都不做

**三种场景，同一套业务代码。**

---

## 4. 为什么有 `tasks.py` 这一层

你可能会问：既然 `cli.py` 和 `gui.py` 都要处理文件，为什么不各自写？

**因为那样会变成两份维护。**

早期版本就是这样，结果出现了经典的「双份维护地狱」：

- CLI 支持去字幕了，GUI 忘了加
- GUI 修了某个 bug，CLI 没修
- 同一个功能，两个入口行为不一致

`tasks.py` 的出现解决了这个问题：

```
cli.py  ──┐
          ├──→  tasks.run_one()  ← 唯一的业务实现
gui.py  ──┘
```

**入口只负责两件事**：
1. 把用户输入解析成 `RunOptions`
2. 把 `FileResult` 渲染成对应的表现形式（终端文字 / 网页 UI）

**所有"怎么处理视频"的逻辑，只有一份，在 `tasks.py` 里。**

> 💡 **判断标准**：如果你在两个入口里都写了「先判断文件存在、再检查有没有音轨」，
> 那这段逻辑就应该下沉到共用层。

---

## 5. 两个核心数据结构

### `RunOptions` —— 输入契约

```python
@dataclass
class RunOptions:
    # 识别
    model: str = "turbo"
    language: Optional[str] = None      # None = 自动检测
    device: str = "auto"
    compute_type: str = "auto"

    # 断句
    max_chars: int = 21                 # 每行最多几个汉字
    max_lines: int = 2
    max_duration: float = 7.0

    # 翻译
    translate: Optional[str] = None     # None = 不翻译
    bilingual: bool = False
    mt_backend: str = "auto"            # auto / offline / llm

    # 输出
    formats: List[str] = ["srt"]
    burn: bool = False
    mux: bool = False
    desub: bool = False                 # 去原字幕
    ...
```

**它是纯数据**，没有任何行为。这让它可以：
- 从命令行参数构造
- 从 JSON 构造（GUI 传来的）
- 轻松序列化 / 打印 / 比较

### `FileResult` —— 输出契约

```python
@dataclass
class FileResult:
    input: str
    ok: bool = False
    error: str = ""

    duration: float = 0.0
    cues: int = 0                       # 生成了多少条字幕
    language: str = ""
    elapsed: float = 0.0
    speed: float = 0.0                  # 实时倍率
    device: str = ""

    outputs: List[str] = ...            # 所有产出文件
    burned: str = ""                    # 烧录结果
    muxed: str = ""                     # 多轨封装结果
    cleaned: str = ""                   # 去字幕后的干净视频
    subtitle_preview: List[dict] = ...  # 前 200 条字幕（GUI 预览用）
```

**关键点：`ok=False` 时也不抛异常。**

因为它代表"这个文件处理完了，结果是失败"。批量场景下，
一个失败不能中断整批。异常只在**程序级错误**（如配置错误）时才抛。

---

## 6. 一次完整的调用链

以 `subgen.bat 视频.mp4 -t en --burn` 为例：

```
1. cli.py 解析参数
   → RunOptions(model="turbo", translate="en", burn=True, ...)

2. cli.py 调 run_one(src, opts, reporter=CliReporter())

3. run_one 内部：
   ├─ ⓪ 去字幕（如果 opts.desub）      → 产出 xxx.clean.mp4
   ├─ ① audio.extract_audio()          → 产出 /tmp/xxx.wav
   ├─ ② engine.Transcriber.load()      → 加载模型到显存
   │     engine.Transcriber.transcribe() → List[Token]
   ├─ ③ segmenter.build_cues()         → List[Cue]
   ├─ ④ translator.translate()         → List[Cue]（带 trans_lines）
   ├─ ⑤ formats.write_subtitle() × N   → xxx.srt / xxx.en.srt
   └─ ⑥ audio.burn()                   → xxx.hardburn.mp4

4. 返回 FileResult(ok=True, outputs=[...], cues=428, ...)

5. cli.py 打印结果
```

**注意 ① 那一步用的是 `work_src` 而不是原始 `src`**：

```python
work_src = src
if opts.desub:
    work_src = target          # 去字幕后的干净视频
...
ff.burn(work_src, burn_srt, target)   # ← 基于干净视频烧录
```

**如果这里用 `src`，新字幕会烧在旧字幕上。** 这个顺序是踩过坑才理清的。

---

## 7. 目录结构对应关系

```
subtitle-gen/
├── subgen/                  ← 核心引擎（不依赖界面）
│   ├── audio.py             ① 音视频
│   ├── engine.py            ② 识别
│   ├── models.py            模型管理（横向）
│   ├── segmenter.py         ③ 断句 ★
│   ├── formats.py           ⑤ 格式
│   ├── translator.py        ④ 翻译
│   ├── desub.py             ⓪ 去字幕
│   ├── tasks.py             编排层
│   ├── reporter.py          进度（横向）
│   ├── cli.py               ┐
│   ├── gui.py               ├ 入口
│   ├── gui_main.py          │
│   └── review.py            ┘
├── webui/                   ← 界面（纯静态）
│   ├── index.html           主界面
│   └── review.html          校对工作台
├── tests/                   ← 验证脚本
├── models/                  ← 模型缓存（不进 git）
└── out/                     ← 输出（不进 git）
```

**注意 `subgen/` 里没有任何前端依赖**。这意味着核心引擎可以单独打包成库用。

---

## 8. 一个设计上的取舍

**问题**：为什么 `cli.py` 有 950 行？它不是应该只做参数解析吗？

**现实**：它还包含了一批**只有命令行才需要**的功能：

- 终端进度条渲染（`CliReporter` 也在 reporter.py，但格式化在这里）
- 批量任务的汇总表格
- `--check` 自检输出
- `--list-models` / `--list-langs`

这些**本质上是"终端特有的展示逻辑"**，和 GUI 的展示逻辑对等。
所以放在 `cli.py` 是合理的——它们是「渲染层」，不是「业务层」。

> 判断一条代码该放哪：**它是不是只在某一种入口下有意义？**
> 是 → 放那个入口文件；否 → 下沉到 `tasks.py`。

---

## 9. 小结

**三句话**：

1. 六层结构，层间只通过**简单数据结构**通信，没有全局状态
2. `②→③` 是分水岭：ASR 给的是**碎词**，能上屏的字幕是自己**切**出来的
3. `tasks.py` 是唯一业务实现，`cli.py`/`gui.py` 只做参数解析和结果渲染

**下一篇** → [03 · 第一层：音视频处理](03-audio.md)：从最底层开始，看 ffmpeg 是怎么被包起来的。

---

## 附：模块依赖图

```
cli.py ──┐
         ├─→ tasks.py ─┬─→ audio.py
gui.py ──┘             ├─→ engine.py ─→ models.py
                       ├─→ segmenter.py
                       ├─→ translator.py
                       ├─→ formats.py
                       ├─→ desub.py ─→ audio.py
                       └─→ reporter.py

review.py ─→ subio.py, waveform.py, mediaserve.py
gui_main.py ─→ gui.py, dropbridge.py
```

**注意：没有任何模块反向依赖 `tasks.py` 或 `cli.py`/`gui.py`。**
这就是分层干净的表现。
