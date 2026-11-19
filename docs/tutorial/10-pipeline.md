# 10 · 任务编排与分层

> **本篇目标**：理解 CLI 和 GUI 如何共用同一套业务逻辑。
> **前置知识**：读完第 1、2 阶段。
> **对应代码**：`subgen/tasks.py`（588 行）+ `subgen/cli.py`（950 行）
> **耗时**：35 分钟。

---

## 1. 本篇要解决的问题

你有一个命令行版本，现在要加图形界面。

**一种做法**：复制一份 CLI 的代码，改成 GUI 版。

**结果**：两周后你会有两个行为不一致、bug 各修一半的版本。

这不是危言耸听。本项目早期就是这个结构，实际出过这些问题：

| 问题 | 表现 |
|---|---|
| 功能不同步 | CLI 支持去字幕了，GUI 忘了加 |
| 修复不同步 | CLI 修了空文件 bug，GUI 还在崩 |
| 行为不一致 | 同一个视频，两个入口产出的字幕条数不同 |

**本篇讲怎么彻底消除这个问题。**

---

## 2. 核心概念：三层结构

```
   ┌──────────────────────────────────┐
   │  入口层  cli.py / gui.py         │  ← 只做「解析输入」和「渲染输出」
   └────────────┬─────────────────────┘
                │ RunOptions（纯数据）
   ┌────────────▼─────────────────────┐
   │  编排层  tasks.py::run_one       │  ← 唯一的业务实现
   └────────────┬─────────────────────┘
                │ 调用下层函数
   ┌────────────▼─────────────────────┐
   │  能力层  audio / engine /        │  ← 各自独立，互不知道
   │         segmenter / translator…  │
   └──────────────────────────────────┘
```

### 判断标准：改一个功能要改几个文件

| 改动 | 只改 1 处 | 要改 2 处 |
|---|---|---|
| 支持新字幕格式 | ✅ `formats.py` | ❌ |
| 调整断句规则 | ✅ `segmenter.py` | ❌ |
| 新增一个 CLI 参数 | ✅ `cli.py` | ✅ 正常 |

**如果同一个功能逻辑出现在两个文件里，就是架构出了问题。**

---

## 3. 代码走读：`run_one` 的八个步骤

```python
def run_one(src, opts, *, transcriber=None, translator=None,
            quiet=False, model_path=None) -> FileResult:
```

### 步骤 0：去字幕（可选但必须最先做）

```python
work_src = src
if opts.desub:
    ...
    target = default_output(src, suffix=".clean")
    remove_subtitles(src, target, regions, method=opts.desub_method)
    work_src = target
```

**这个顺序至关重要。**

```python
work_src = src          # 默认用原始文件
if opts.desub:
    work_src = target   # 去字幕后，后续全用干净视频
```

后面所有操作（识别、烧录、封装）**都用 `work_src` 而不是 `src`**：

```python
ff.burn(work_src, burn_srt, target)   # ← 如果这里写 src，新字幕会烧在旧字幕上
```

> 💡 **这类"变量被替换后要贯穿全程"的逻辑，是最容易漏的地方。**
> 建议给变量取明确的名字（`work_src` 比 `src` 好），提醒自己"这是处理后要用的"。

### 步骤 1-2：抽音频 + 识别

```python
rep.stage("audio", "抽取音轨（16 kHz 单声道）…", total=1.0)
tmp = ff.extract_audio(src, sample_rate=16000, mono=True, ...)

rep.stage("asr", "语音识别中…", total=dur if dur > 0 else 1.0)
t_res = transcriber.transcribe(tmp, ...)
tokens = t_res.tokens
```

**注意 `total=dur`**：进度条按"音频秒数"推进，所以能算出准确百分比。

### 步骤 3：断句

```python
rep.stage("export", ...)   # 断句很快，不单独设阶段
cues = build_cues(tokens, max_line_width=..., max_lines=..., max_dur=...)
```

**断句不设独立阶段**，因为它在几百毫秒内完成，进度条来不及更新。
直接归到"导出"阶段更合理。

### 步骤 4：翻译（可选）

```python
if opts.translate:
    rep.stage("translate", f"翻译 {lang_label(src_lang)} → {lang_label(tgt)}…")
    texts = [c.text for c in cues]
    translated = translator.translate(texts)
    for c, t in zip(cues, translated):
        c.trans_lines = wrap_lines(t, ...)
```

**`zip(cues, translated)` 依赖两个列表长度一致。**
这就是 07 篇讲的"空行占位 + 长度校验"为什么必要。

### 步骤 5-7：输出、烧录、封装

```python
# 5) 导出字幕
for suffix, vcues, vlang in variants:
    for fmt in opts.formats:
        write_subtitle(out, vcues, fmt, **kw)

# 6) 烧录（可选）
if opts.burn and is_video(src):
    ff.burn(work_src, burn_srt, target, font=opts.font)

# 7) 多轨封装（可选）
if opts.mux and is_video(src):
    ff.mux_multi(work_src, tracks, target, default_lang=tracks[0][1])
```

---

## 4. 核心概念：容错设计

批量处理 40 个文件，跑 3 小时。**一个失败不能让整批崩掉。**

```python
for i, p in enumerate(paths, 1):
    try:
        res = run_one(p, opts, reporter=rep, ...)
    except Cancelled:
        break                          # 用户取消，正常退出
    except Exception as e:
        res = None
        err = f"{type(e).__name__}: {e}"
        self._emit(f"window.onFileDone && ... ok:False ...")
        fail_count += 1
        continue                       # ← 关键：继续下一个
```

### 三层容错

| 层级 | 处理 | 例子 |
|---|---|---|
| **单个文件内部** | 尝试降级，失败则记录 | 去字幕失败 → 用原视频继续 |
| **单个文件整体** | 捕获异常，记为失败，继续下一个 | 文件损坏 |
| **整批任务** | 汇总结果，报告成功/失败数 | — |

### 一个真实例子

```python
if regions:
    target = default_output(src, suffix=".clean")
    try:
        remove_subtitles(src, target, regions, method=opts.desub_method)
    except Exception as e:
        rep.log(f"去字幕失败（继续处理原视频）：{e}")
    else:
        work_src = target
```

**去字幕失败不中断，继续用原视频处理。**

理由：去字幕是"增强功能"，失败了顶多是新老字幕重叠，
但**总比整个文件处理失败好**。

> 💡 **降级处理的原则**：核心功能失败 → 失败；增强功能失败 → 降级继续。

---

## 5. 代码走读：入口层该做什么

### `cli.py` 的职责

```python
# 1. 解析参数
parser = argparse.ArgumentParser()
parser.add_argument("inputs", nargs="+")
parser.add_argument("-m", "--model", default="turbo")
...

# 2. 构造 RunOptions
opts = RunOptions(
    model=args.model,
    formats=args.formats,
    translate=args.translate,
    ...
)

# 3. 遍历文件，调 run_one
for f in args.inputs:
    res = run_one(f, opts, reporter=CliReporter())
    results.append(res)

# 4. 渲染结果（终端表格）
print(f"完成：成功 {ok}  失败 {fail}")
```

**就这四件事。不含任何"怎么识别、怎么断句"的逻辑。**

### `gui.py` 的职责

```python
class GuiApi:
    def start_job(self, payload):
        """启动后台线程，立即返回。"""
        t = threading.Thread(target=self._run_job, args=(paths, s))
        t.start()
        return {"ok": True}

    def _run_job(self, paths, s):
        opts = self._build_options(paths, s)     # ① JSON → RunOptions
        for p in paths:
            res = run_one(p, opts, reporter=rep)  # ② 同样的 run_one
            self._emit_file_done(res)             # ③ 转成 JS 事件
```

**关键：`gui.py` 也调 `run_one`，用同样的 `RunOptions`。**

区别只在"输出怎么呈现"：
- CLI → 打印到终端
- GUI → 发 JS 事件给网页

### `_build_options` —— 唯一的转换层

```python
def _build_options(self, paths, s: Dict[str, Any]) -> RunOptions:
    """把界面传来的 dict 转成 RunOptions。

    这是唯一的转换点。所有字段的默认值和类型转换都在这里，
    不允许别处再解析一遍 dict。
    """
    return RunOptions(
        model=s.get("model") or "turbo",
        formats=list(s.get("formats") or ["srt"]),
        ...
    )
```

**为什么要强调"唯一转换点"？**

因为 `s` 是从 JS 传过来的 JSON，类型可能不对（数字变字符串、布尔变 "true"）。
如果多处解析，每处的容错处理可能不一致。

---

## 6. 一个反例：什么不该下沉

`cli.py` 有 950 行，看起来不符合"入口层应该很薄"。

看它包含什么：

| 内容 | 该在哪 |
|---|---|
| 参数解析 | ✅ cli.py |
| `--check` 自检输出 | ✅ cli.py（CLI 特有） |
| `--list-models` 表格 | ✅ cli.py（CLI 特有） |
| 批量汇总表格 | ✅ cli.py（终端呈现） |
| **识别/断句/翻译逻辑** | ❌ 一条都不该有 |

**判断标准**：这段代码**是不是只在命令行场景下有意义**？

- "打印进度条到终端" → 只有 CLI 有意义 → 放 cli.py
- "抽取音频" → 两个入口都要 → 放 tasks.py

---

## 7. 依赖注入：`reporter` 的设计

```python
def run_one(src, opts, *, reporter: Optional[Reporter] = None, ...):
    ...
    rep = reporter or NullReporter()     # 不传就用空实现
```

**三种使用场景**：

| 场景 | 传入 | 效果 |
|---|---|---|
| CLI | `CliReporter()` | 终端进度条 |
| GUI | `CallbackReporter(...)` | 推 JS 事件 |
| 测试 | 不传（`NullReporter`） | 什么都不做 |

**同一套业务代码，三种输出方式。**

### `Reporter` 接口

```python
class Reporter:
    def log(self, msg): ...
    def stage(self, stage, label, *, total=0.0): ...
    def update(self, current, *, extra=""): ...
    def file_start(self, index, count, name): ...
    def file_done(self, index, count, name, ok, msg=""): ...
    def finish(self): ...
    def check_cancel(self): ...
```

**业务代码只调这几个方法，完全不知道背后是终端还是网页。**

> 💡 这是"面向接口编程"的实际价值：
> 加一个新入口（比如 Web API）时，**核心代码一行都不用改**。

---

## 8. 动手练习

**练习 1：验证两个入口行为一致**

```bash
# CLI
subgen.bat 测试.mp4 -f srt -o ./cli_out

# GUI（用 Python 直接调）
python -c "
import sys; sys.path.insert(0, '.')
from subgen.gui import GuiApi
api = GuiApi()
opts = api._build_options(['测试.mp4'], {'formats':['srt'], 'output':'./gui_out'})
print(opts)
"
```

对比两次生成的 `.srt`——**内容应该完全一致**。

**练习 2：找到"只该在一个入口存在"的代码**

打开 `cli.py`，随便找 3 个函数，判断：
- 它是不是 CLI 特有？
- 如果 GUI 也需要，该移到哪？

**练习 3：模拟一个失败的增强功能**

把 `--desub` 的区域设成一个荒谬的值：

```bash
subgen.bat 测试.mp4 --desub "99999,99999,10,10" -f srt
```

观察：**去字幕失败了，但主流程继续跑完了。**

**练习 4：故意制造双份逻辑**

假设你要加一个"输出文件名加时间戳"的功能。
试着分别在 `cli.py` 和 `gui.py` 里实现，然后想想——
**如果以后规则改了，你要改几个地方？**

---

## 9. 小结

**三句话**：

1. **`tasks.run_one` 是唯一业务实现**，入口层只做参数解析和结果渲染
2. **增强功能失败要降级继续**（去字幕失败 → 用原视频），核心功能失败才中断
3. **依赖注入让同一套代码服务三种场景**（CLI/GUI/测试）

**下一篇** → [11 · 进度上报与跨线程通信](11-progress.md)：
这是本项目**踩坑最多**的地方。pywebview 的跨线程问题会让程序
"看起来卡死但没有任何报错"——极其难查。
