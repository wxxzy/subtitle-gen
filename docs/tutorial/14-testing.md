# 14 · 测试策略

> **本篇目标**：怎么测一个"输出是视频/字幕"的项目。
> **前置知识**：读完前面所有篇。
> **对应代码**：`tests/`（2057 行，13 个脚本）
> **耗时**：30 分钟。

---

## 1. 本篇要解决的问题

这个项目的输出是**视频和字幕文件**，不是函数返回值。

**怎么断言一个 `.srt` 是对的？**

- 条数对不对？
- 时间戳有没有重叠？
- 有没有断在单词中间？
- 行首有没有标点？

**这些都是"质量"问题，没有唯一标准答案。**

而且最麻烦的是：**很多问题不会报错，只是结果不对**。
13 篇里的坑 7、9 都是这样。

**所以测试策略的核心不是"覆盖率"，而是"守住容易静默失效的地方"。**

---

## 2. 核心概念：三层测试

```
┌─────────────────────────────────────┐
│  ③ 界面测试（无头浏览器）              │  ← 慢，但能抓前端 bug
│     4 个脚本，约 500 行                │
├─────────────────────────────────────┤
│  ② 集成测试（真实文件）                │  ← 慢，验证端到端
│     3 个脚本，约 300 行                │
├─────────────────────────────────────┤
│  ① 单元测试（纯函数）                  │  ← 快，可频繁跑
│     test_smoke.py，93 项，约 450 行    │
└─────────────────────────────────────┘
```

| 层 | 耗时 | 什么时候跑 | 抓什么 |
|---|---|---|---|
| 单元 | < 1 秒 | 每次改代码 | 算法错误 |
| 集成 | 约 30 秒 | 改核心链路后 | 流程断裂 |
| 界面 | 约 10 秒 | 改前端后 | DOM/JS 错误 |

---

## 3. 第一层：单元测试

**测什么**：纯函数，不碰文件、不碰 GPU。

**这是性价比最高的**——跑得快、定位准。

### 例子：折行算法

```python
def test_wrap() -> None:
    print("\n[1] 折行")

    # 短文本不折行
    assert wrap_lines("你好", max_width=42) == ["你好"]

    # 超长必须折成两行
    long = "这是一段很长的文字" * 5
    lines = wrap_lines(long, max_width=42, max_lines=2)
    assert len(lines) == 2

    # 每行宽度不超过限制
    for ln in lines:
        assert text_width(ln) <= 42

    # 行首不能是标点
    text = "他说这里很危险，我们快走吧，不要回头。"
    for w in (12, 16, 20, 24):
        parts = wrap_lines(text, max_width=w)
        for p in parts[1:]:
            assert p[0] not in NO_LINE_START
```

**注意最后一个断言**——这是**核心规则**的守护。
只要有这条断言，将来谁改坏了"行首不能标点"，测试立刻红。

### 例子：单词完整性

```python
def test_word_integrity() -> None:
    print("\n[5b] 单词完整性（不许断词）")
    text = "The quick brown fox jumps over the lazy dog"
    for w in range(8, 40, 2):
        for line in wrap_lines(text, max_width=w, max_lines=3):
            # 折出来的每一段，都必须是原句里的连续单词
            for word in line.split():
                assert word in text.split()
```

**这个测试抓住了 06 篇讲的那个点**：英文绝不能把单词劈成两半。

### 例子：SRT 可解析性

```python
def test_srt_parseable() -> None:
    print("\n[4] SRT 可解析性")

    srt = to_srt(cues)

    # 编号连续
    blocks = [b for b in srt.split("\n\n") if b.strip()]
    for i, b in enumerate(blocks, 1):
        lines = b.strip().split("\n")
        assert lines[0] == str(i)

        # 时间行格式正确
        assert "-->" in lines[1]
        m = re.match(r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})", lines[1])
        assert m

        # 开始 < 结束
        start, end = m.groups()
        assert start < end
```

**用正则校验格式**——这能抓住"时间格式写错"（06 篇讲的那个坑）。

---

## 4. 第二层：集成测试

**测什么**：真实文件走完整流程。

**代价高**（要跑 GPU、要几十秒），**但能发现单元测试抓不到的问题**。

### 例子：端到端 GUI 流程

```python
def verify_e2e_gui():
    api = GuiApi()
    api.start_job({
        "paths": [VIDEO],
        "options": {"model": "turbo", "formats": ["srt"], "output": "out/e2e"},
    })

    # 轮询事件，直到 onJobDone
    done = False
    events = []
    t0 = time.time()
    while time.time() - t0 < 300:
        for e in api.poll_events():
            events.append(e.get("js", ""))
            if "onJobDone" in e["js"]:
                done = True
        if done:
            break
        time.sleep(0.25)

    assert done, "任务未完成"
    assert any("onFileDone" in e for e in events), "没收到文件完成事件"
```

**这个测试守的是**：整条链路能跑通，事件能送达。

### 例子：心跳生命周期（守坑 7）

```python
def test_heartbeat_lifecycle():
    events = []
    rep = CallbackReporter(on_progress=lambda p: events.append(p.stage))

    rep.stage("export", "导出字幕文件…")
    time.sleep(1.4)
    during = len(events)
    assert during >= 1, "心跳没在跑"

    rep.file_done(1, 1, "x.mp4", True)
    time.sleep(1.2)
    after_done = len(events)
    time.sleep(1.5)
    assert len(events) == after_done, "file_done 后心跳没停"
```

**这个测试专门守 13 篇的坑 7。**

**为什么要专门写一个测试？**

因为这类 bug **不会报错**——任务正常完成，只是状态栏不停。
如果不测，它会一直潜伏到用户发现。

> 💡 **判断一个地方要不要写测试**：问自己
> **"如果这里坏了，会不会报错？"**
> 不会报错 → 必须测。

---

## 5. 第三层：界面测试

**测什么**：真实启动 pywebview，回读 DOM。

### 技术要点

用无头浏览器挂错误钩子：

```python
ERROR_HOOK = """
<script>
window.__errs = [];
window.addEventListener('error', (e) => {
  window.__errs.push('ERROR: ' + e.message);
});
window.addEventListener('unhandledrejection', (e) => {
  window.__errs.push('PROMISE: ' + e.reason);
});
</script>
"""
```

注入 mock API：

```python
MOCK = """
<script>
window.pywebview = { api: {
  get_env: async () => ({ffmpeg: true, device: 'cuda', ...}),
  load_settings: async () => ({model: 'turbo', ...}),
  poll_events: async () => [],
  ...
}};
setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 40);
</script>
"""
```

用 `--dump-dom` 取回结果：

```python
subprocess.run([
    EDGE, "--headless=new", "--disable-gpu",
    "--virtual-time-budget=7000", "--dump-dom",
    f"file:///{page}",
])
```

结果写进 `document.title`，从 DOM 里正则取回：

```javascript
document.title = 'RESULT:' + JSON.stringify(R);
```

### 这个技巧的适用范围

| 能抓什么 | 抓不到什么 |
|---|---|
| id 拼错 | 视觉样式问题 |
| 函数未定义 | 性能问题 |
| 时序问题 | 真实的用户操作路径 |
| 初始化异常 | — |

**它抓的是"逻辑错误"，不是"好不好看"。**

---

## 6. 核心概念：重点测"静默失效"

这是本项目的测试哲学。

### 什么是静默失效

**不报错，但结果不对。**

13 篇里的例子：

| 坑 | 现象 | 报错吗 |
|---|---|---|
| 7 心跳没停 | 状态栏永远"处理中" | ❌ |
| 8 时间基准错 | 显示 47:39 | ❌ |
| 9 别名未解析 | 下载了错的模型 | ❌（只是慢/占空间） |
| 12 强缓存 | 看到旧界面 | ❌ |

**这类 bug 最危险**——它们能通过"跑一遍看看"的验证。

### 对策：给它们写专门的测试

| 坑 | 测试文件 |
|---|---|
| 7、8 | `verify_progress_lifecycle.py` |
| 12 | `verify_gui_fresh.py` |
| — | `verify_llm_config.py`（配置读写的静默错误） |

**这些测试的名字都带 `verify_` 而不是 `test_`**，
表示它们验证的是"行为契约"而不是"函数输出"。

---

## 7. 核心概念：测试脚本自身的坑

**坑 11 讲的**：测试脚本自己坏了，却以为是产品回归。

### 三个防坑要点

**① 不要用固定 `sleep`**

```python
# ❌ 机器忙时不够
time.sleep(4)
r = win.evaluate_js(...)

# ✅ 轮询等待
for _ in range(50):
    r = win.evaluate_js("document.readyState")
    if r == "complete":
        break
    time.sleep(0.4)
```

**② 不要手拼分隔符**

```python
# ❌ 容易漏逗号
js = "{...}" + ",".join(props) + ", __len: ..."

# ✅ 用列表 join
props.append("__len: ...")
js = "{" + ", ".join(props) + "}"
```

**③ 测试数据要隔离**

```python
# ❌ 输入输出同目录，通配符会取到产物
V = sorted(glob.glob("data/*.mp4"))[0]

# ✅ 固定文件名，或者输入输出分目录
V = "data/测试素材.mp4"
```

---

## 8. 测试清单

一个完整的测试跑法：

```bash
# 1. 单元测试（快，改代码就跑）
.venv/Scripts/python.exe tests/test_smoke.py

# 2. 核心链路验证
.venv/Scripts/python.exe tests/verify_progress_lifecycle.py
.venv/Scripts/python.exe tests/verify_e2e_gui.py

# 3. 界面验证（改前端后）
.venv/Scripts/python.exe tests/verify_gui_fresh.py
.venv/Scripts/python.exe tests/verify_llm_config.py
.venv/Scripts/python.exe tests/verify_review.py
.venv/Scripts/python.exe tests/verify_timeline.py
```

### 现有测试一览

| 文件 | 类型 | 守什么 |
|---|---|---|
| `test_smoke.py` | 单元 | 断句/折行/格式/翻译/纠错，93 项 |
| `verify_progress_lifecycle.py` | 集成 | 心跳停止、时间基准 |
| `verify_e2e_gui.py` | 集成 | 完整流程、事件送达 |
| `verify_gui_fresh.py` | 界面 | 加载的是最新界面（防缓存） |
| `verify_llm_config.py` | 界面 | 大模型配置界面 |
| `verify_review.py` | 界面 | 校对工作台 |
| `verify_timeline.py` | 界面 | 时间轴交互 |
| `verify_log.py` | 界面 | 日志面板 |
| `verify_thread_push.py` | 集成 | 跨线程事件推送 |
| `preview_*.py` | 工具 | 生成界面预览图（不 assert） |

---

## 9. 什么没测（诚实的部分）

| 没测的 | 为什么 | 风险 |
|---|---|---|
| ASR 识别准确率 | 没有标注数据集 | 中 |
| 翻译质量 | 主观判断 | 中 |
| 去字幕视觉质量 | 需要人眼看 | 低（有预览图） |
| 极端大文件（4 小时+） | 太慢 | 中 |
| 并发多任务 | 设计上不支持 | 低 |

**"没测"不等于"不重要"**，而是**投入产出比低**。
这些更适合人工抽检。

> 💡 **测试覆盖率不是目标。**
> 目标是「**关键路径 + 静默失效点**都有守护」。

---

## 10. 动手练习

**练习 1：跑一遍全部测试**

```bash
.venv/Scripts/python.exe tests/test_smoke.py
```

看 93 项全过。然后**故意改坏一个规则**——比如把
`NO_LINE_START` 清空，再跑一次，看测试是否报错。

**这个练习验证"测试真的在守"**，而不是摆设。

**练习 2：写一个针对坑 7 的反向测试**

把 `rep.file_done(...)` 注释掉，跑
`verify_progress_lifecycle.py`，**确认它失败**。

**能失败的测试才是有效的测试。**

**练习 3：给新功能加测试**

假设你要加一个"自动去掉句末语气词"的功能。
写测试：

```python
def test_strip_particles():
    assert strip("你好啊") == "你好"
    assert strip("真的吗") == "真的"
    assert strip("走吧") == "走"
    assert strip("正常句子") == "正常句子"    # 不该误删
```

**注意最后一个断言**——**测边界比测正常路径更重要。**

**练习 4：检查测试是否会相互污染**

连续跑两次 `verify_e2e_gui.py`，确认第二次也通过
（不会因为第一次的产物而失败）。

---

## 11. 小结

**三句话**：

1. **三层测试**：单元（快）+ 集成（真实文件）+ 界面（无头浏览器回读 DOM）
2. **重点测静默失效**——不报错但结果不对的地方，必须专门写测试守
3. **测试脚本自己也会坏**——用轮询代替 sleep、用 join 代替手拼、数据要隔离

---

## 写在最后

13 篇和 14 篇加起来，是这个项目里**最有价值的部分**。

代码可以照着抄，但"哪里会出问题、怎么发现的、当时怎么想错的"——
这些只能从真实经历里来。

如果你正在做类似的项目，希望这些坑能帮你省下几个小时。

**回到 [目录](README.md)**
