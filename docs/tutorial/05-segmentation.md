# 05 · 第三层：智能断句 ⭐

> **本篇目标**：理解本项目最核心的算法——把 ASR 的原始词流，切成能上屏的字幕。
> **前置知识**：读完 04 篇，知道 ASR 输出的是 `List[Token]`。
> **对应代码**：`subgen/segmenter.py`（293 行）
> **耗时**：60 分钟（这篇可以反复读）

---

## 1. 本篇要解决的问题

先看 ASR 原始输出长什么样。

Whisper 返回的是 **segment**，按 30 秒窗口切：

```python
segment = {
    "text": "电梯里年轻男子带双马尾的女友回家见父母两人表情紧张似乎有什么难言之隐",
    "start": 0.0,
    "end": 9.2,
}
```

直接拿去当字幕会怎样？

```
┌────────────────────────────────────────────┐
│ 电梯里年轻男子带双马尾的女友回家见父母两人表情紧张   │
│ 似乎有什么难言之隐                             │
└────────────────────────────────────────────┘
```

**三个问题**：

| 问题 | 后果 |
|---|---|
| 太长（38 个字） | 占满大半个屏幕，挡住画面 |
| 一行装不下 | 自动折行位置不可控，可能断在词中间 |
| 持续 9.2 秒 | 观众盯着同一句话太久，阅读节奏崩了 |
| **没有标点**（口语场景） | 不知道在哪停顿 |

**所以「字幕不是转录」。** 转录要求一字不差；字幕要求**可读**。

本项目做的就是后者：把碎词重新组织成**符合阅读节奏**的短句。

---

## 2. 核心概念：三重约束

这是整个算法的基础。切分时同时受三个约束：

```
        标点约束（语义边界）
              ↓
   ┌──────────────────────┐
   │   token token token  │
   └──────────────────────┘
        ↑                ↑
   长度约束          时长约束
   （屏幕装得下）    （观众读得完）
```

| 约束 | 阈值 | 为什么 |
|---|---|---|
| **长度** | 每行 ≤ 21 汉字，最多 2 行 | 竖屏手机屏宽有限 |
| **时长** | 每条 1~7 秒 | 短于 1 秒看不清，长于 7 秒读得烦 |
| **标点** | 句末必断，句中满了就断 | 语义完整优先 |

这三个数字**不是拍脑袋定的**，是行业惯例：

- **21 汉字/行**：竖屏 1080 宽，字号约 48px 时，一行约能放 20~22 个汉字
- **2 行**：超过 2 行会挡住画面主体
- **7 秒上限**：人阅读速度约 5~8 字/秒，7 秒能读完约 40 字——正好是 2 行的容量

> 💡 **注意这三个数是联动的**：
> 21 字 × 2 行 = 42 字，人读完约 6 秒，所以时长上限是 7 秒。**自洽的。**

---

## 3. 核心概念：宽度不是字数

这是中英混排的关键。

```python
def char_width(ch: str) -> int:
    """单字符显示宽度：全角 2，半角 1。"""
    o = ord(ch)
    if o < 0x1100:
        return 1
    if (0x1100 <= o <= 0x115F       # 韩文字母
        or 0x2E80 <= o <= 0xA4CF    # CJK 部首、汉字
        or 0xAC00 <= o <= 0xD7A3    # 韩文音节
        or 0xF900 <= o <= 0xFAFF    # CJK 兼容汉字
        or 0xFE30 <= o <= 0xFE6F    # CJK 兼容形式
        or 0xFF00 <= o <= 0xFF60    # 全角字符
        or 0xFFE0 <= o <= 0xFFE6    # 全角符号
        or 0x20000 <= o <= 0x3FFFD  # CJK 扩展 B 及以后
    ):
        return 2
    return 1
```

**为什么要按宽度算而不是按字符数？**

看这个例子：

```
"你好世界"           → 4 个字符，宽度 8
"Hello world"       → 11 个字符，宽度 11
```

如果按字符数限制 21，两行都会认为"没超"。
但实际显示时，`你好世界` 占的**屏幕宽度**和 `Hello wo` 差不多。

**按宽度算，才能保证视觉上每行一样长。**

### 这个函数其实是简化版

真实的中日韩宽度判断比这复杂（要考虑 Unicode East Asian Width 属性、
emoji 宽度、组合字符等）。这里用**码点区间**的简化判断，覆盖了：

- 汉字（基本区 + 扩展 A/B）
- 日文假名
- 韩文
- 全角标点

对字幕场景足够。**够用就好，不用追求 Unicode 完备性。**

> ⚠️ 已知局限：emoji 会被算成 1（实际通常是 2）。
> 字幕里 emoji 很少见，没为此增加复杂度。

---

## 4. 代码走读：`wrap_lines` —— 打分找断点

这是折行的核心，也是最巧妙的地方。

```python
def wrap_lines(text: str, max_width: int = 42, max_lines: int = 2) -> List[str]:
    """折行。max_width 单位为半角字符数：42 约等于 21 个汉字。

    断点优先级：句末标点 > 句中标点 > 空格 > 字符边界（仅 CJK 允许）。
    """
```

### 第一步：给每个可能的断点打分

```python
best_i, best_score = None, -1.0
for i in range(1, n):
    w1 = text_width(text[:i])
    if w1 > max_width:          # 超过最大宽度，后面都不用看了
        break
    ch = text[i - 1]            # 断点前的那个字符
    if ch in SENT_END:
        bonus = 40              # 。！？
    elif ch in CLAUSE_END:
        bonus = 28              # ，、；
    elif ch == " ":
        bonus = 20              # 空格
    elif ch.isspace():
        bonus = 14
    else:
        bonus = 0               # 硬切字符
    score = w1 + bonus
    if score > best_score:
        best_score, best_i = score, i
```

**这个打分函数值得细说。**

`score = 已用宽度 + 标点奖励`

意思是：**优先在宽度用得差不多的地方断，但标点可以"加分"抵消宽度不足**。

举例，假设 `max_width = 20`（10 个汉字），文本是：

```
"他说这里很危险，我们快走吧"
     ↑8字(16)  ↑9字(18)  ↑10字(20)
```

| 断点位置 | 宽度得分 | 标点奖励 | 总分 |
|---|---|---|---|
| 第 8 字后（"危险"后） | 16 | 0（无标点） | 16 |
| **第 9 字后（"，"后）** | **18** | **28（句中标点）** | **46** ✓ |
| 第 10 字后（满宽） | 20 | 0 | 20 |

**选择了第 9 字后**——在逗号处断。虽然比满宽少 2，但语义完整得多。

再看句末标点的威力：

```
"他走了。我们开始吧"    max_width=20
```

| 断点 | 宽度 | 奖励 | 总分 |
|---|---|---|---|
| "走"后 | 6 | 0 | 6 |
| **"。"后** | **8** | **40** | **48** ✓ |
| 满宽处 | 20 | 0 | 20 |

**40 分的奖励足以压过任何宽度差异**，所以句末标点几乎总是首选断点。
这正是我们想要的——**一句话完整地待在一行里**。

### 第二步：带空格的语言，吸附到单词边界

```python
if spaced:
    # 断点落在单词中间时，退到前一个空格处更自然
    if 0 < pos < n and _is_word_char(text[pos-1]) and _is_word_char(text[pos]):
        sp = text.rfind(" ", 0, pos)
        pos = sp + 1 if sp > 0 else pos
```

**英文绝不能把单词劈成两半。**

```
❌ 断在这里
   The quick brown fo
   x jumps over

✅ 应该这样
   The quick brown
   fox jumps over
```

判断方法：如果断点前后**都是字母/数字**，说明切在单词中间了，
就往前找最近的空格。

### 第三步：避免行首标点

```python
guard = 0
while pos < n and guard < n:
    rest = text[pos:].strip()
    if not rest or rest[0] not in NO_LINE_START:
        break
    pos += 1
    guard += 1
```

**中文排版有个硬规则：行首不能是标点。**

```
❌ 这样很难看
   他说这里很危险
   ，我们快走吧

✅ 应该这样
   他说这里很危险，
   我们快走吧
```

做法很简单：如果下一行开头是标点，就**把断点往后挪一位**，
把标点拽到上一行末尾。

`guard` 是循环保护，防止极端情况死循环。

### 第四步：均衡两行宽度

```python
if max_lines == 2:
    # 第二行仍超宽则继续右移，让两行宽度更均衡
    guard = 0
    while pos < n and text_width(text[pos:].strip()) > max_width and guard < n:
        if spaced:
            nxt = text.find(" ", pos)
            pos = (nxt + 1) if nxt != -1 else n
        else:
            pos += 1
        guard += 1
    return [text[:pos].strip(), text[pos:].strip()]
```

**为什么需要这一步？**

因为第一步的打分会选"最合适"的断点，但可能留下一个超宽的第二行：

```
第一行：19 字（接近满宽）
第二行：25 字（超了）
```

所以要继续右移断点，直到第二行也装得下。**注意英文是按单词整块挪动的**，
避免二次切碎。

---

## 5. 代码走读：`build_cues` —— 切分主循环

这是把 token 流切成 cue 的主函数。

```python
def build_cues(tokens, *, max_line_width=42, max_lines=2,
               max_dur=7.0, min_dur=1.0, max_gap=1.2, min_chars=1):
    total_limit = max_line_width * max_lines    # 42 * 2 = 84 半角 = 42 汉字
    cues = []
    cur = []          # 当前正在累积的 token
    cur_w = 0         # 当前累积的宽度
```

### `flush()` —— 把当前累积的吐成一条 cue

```python
def flush():
    nonlocal cur, cur_w
    if not cur:
        return
    text = smart_join(cur)
    if text_width(text) < min_chars:
        # 太短（多为噪声或纯标点），并到上一条
        if cues:
            cues[-1].end = max(cues[-1].end, cur[-1].end)
            cues[-1].tokens.extend(cur)
            cues[-1].lines = wrap_lines(smart_join(cues[-1].tokens), ...)
        cur, cur_w = [], 0
        return
    cues.append(Cue(
        start=cur[0].start,
        end=cur[-1].end,
        lines=wrap_lines(text, max_line_width, max_lines),
        tokens=list(cur),
    ))
    cur, cur_w = [], 0
```

**注意"太短就并到上一条"这个处理。**

ASR 偶尔会吐出孤立的语气词（"嗯"、"啊"），单独成一条会闪一下。
合并到上一条既避免了闪烁，又让句子更完整。

### 主循环：三种断句触发条件

```python
for tok in tokens:
    s = (tok.text or "").strip()
    if not s:
        continue

    # ── 条件 1：静音过长，且当前 cue 已经够长
    if cur and (tok.start - cur[-1].end) > max_gap:
        if (cur[-1].end - cur[0].start) >= min_dur:
            flush()

    w = text_width(s) + (1 if _needs_space(smart_join(cur), s) else 0)

    # ── 条件 2：超长 或 超时
    if cur and (cur_w + w > total_limit or (tok.end - cur[0].start) > max_dur):
        flush()

    cur.append(tok)
    cur_w += w

    # ── 条件 3：标点
    if s[-1] in SENT_END:
        flush()                              # 句末标点，立刻断
    elif s[-1] in CLAUSE_END and cur_w >= max_line_width:
        flush()                              # 句中标点 + 已满一行
```

**三个条件的优先级和意图**：

| 条件 | 触发 | 意图 |
|---|---|---|
| 静音 > 1.2s | 说话人停顿了 | 语义上确实说完了 |
| 超长/超时 | 装不下了 | 物理约束 |
| **句末标点** | 有 `。！？` | **最强信号，无条件断** |
| 句中标点 + 满一行 | 有 `，` 且够长 | 顺手断，读起来舒服 |

**注意"条件 1"里的双重判断**：

```python
if cur and (tok.start - cur[-1].end) > max_gap:
    if (cur[-1].end - cur[0].start) >= min_dur:    # ← 还有这层
        flush()
```

为什么不满足静音就直接断？

因为短剧里有大量**短促对话**：

```
"你！"（停 1.5 秒）"我怎么了？"
```

如果一有静音就断，"你！"会变成独立的一条 cue，只有 0.5 秒——
闪一下就没了。加个 `>= min_dur` 的判断，这种短句就会和后面合并。

> 💡 **这个细节体现了一个原则**：算法约束要服务于**实际场景**。
> 教科书上的"遇到静音就断"在对话密集的素材上会产出垃圾。

---

## 6. 代码走读：`_post_process` —— 修掉三种毛病

即使前面切得再好，还是会有些边角问题。这一步专门修。

```python
def _post_process(cues, *, min_dur, max_dur):
    """修掉过短、重叠、空行。"""
    clean = [c for c in cues if c.lines and any(l.strip() for l in c.lines)]
    clean.sort(key=lambda c: c.start)
```

### 问题 1：显示时间太短

```python
for i, c in enumerate(clean):
    limit = clean[i + 1].start if i + 1 < len(clean) else float("inf")

    if c.duration < min_dur:
        target = min(c.start + min_dur, limit)   # ← 但不能压到下一句
        c.end = max(c.end, target)
```

**为什么要有 `limit`？**

因为补时长可能把当前 cue 拉长到和下一句重叠。所以取
`min(想要延长到的时刻, 下一句开始时刻)`。

### 问题 2：闪烁感

```python
if i + 1 < len(clean):
    gap = clean[i + 1].start - c.end
    if 0 < gap <= 0.4 and (c.end - c.start + gap) <= max_dur:
        c.end = clean[i + 1].start - 0.01
```

**场景**：当前句在 2.0s 结束，下一句在 2.3s 开始。中间 0.3 秒字幕消失。

观众看到的是：**字幕闪一下、空白、再出现新字幕**。很不舒服。

解决：把当前句延长到下一句出现前 0.01 秒，**让字幕连续**。

但要注意两个限制：

- `gap <= 0.4` —— 间隔太大就不延长了（那本来就是两个独立的句子）
- `+ gap <= max_dur` —— 延长后不能超过 7 秒上限

### 问题 3：兜底防重叠

```python
if c.end > limit:
    c.end = max(c.start + 0.2, limit - 0.01)
```

万一前面两步还是搞出了重叠，这里强制作保底处理。

`max(c.start + 0.2, ...)` 保证至少有 0.2 秒显示时间，不会出现负时长。

---

## 7. 一个完整例子

输入 token 流（简化的）：

```python
tokens = [
    Token("电梯里", 0.0, 0.8),
    Token("年轻",   0.8, 1.2),
    Token("男子",   1.2, 1.5),
    Token("带着",   1.5, 1.9),
    Token("双马尾", 1.9, 2.4),
    Token("的",     2.4, 2.5),
    Token("女友",   2.5, 2.9),
    Token("回家",   2.9, 3.3),
    Token("见父母", 3.3, 3.9),
    Token("。",     3.9, 4.0),
    ...
]
```

处理过程：

```
1. 累积 "电梯里年轻男子带着双马尾的女友回家见父母"
   宽度 = 10*2 + 9*2 = 38 半角（19 汉字）

2. 遇到 "。" → 在 SENT_END 里 → flush()

3. flush 内部：
   - smart_join → "电梯里年轻男子带着双马尾的女友回家见父母。"
   - wrap_lines(宽 40, max_width=42)
     → 40 <= 42，不需要折行，返回单行

4. 产出 Cue(
     start=0.0, end=4.0,
     lines=["电梯里年轻男子带着双马尾的女友回家见父母。"]
   )

5. _post_process:
   - duration = 4.0，在 [1, 7] 范围内 ✓
   - 下一句 start=4.0，gap=0，不触发延长
```

最终 SR 里就是：

```
1
00:00:00,000 --> 00:00:04,000
电梯里年轻男子带着双马尾的女友回家见父母。
```

**完美。**

---

## 8. 为什么不用现成的断句库

你可能会想：中文断句不是有现成工具吗（结巴分词、pkuseg）？

**因为那是"分词"，不是"断字幕"。**

| 任务 | 目标 |
|---|---|
| 分词 | 把句子切成词：`年轻/男子/带着/双马尾/的/女友` |
| **断字幕** | 把**连续语音**切成**屏幕行**，受长度/时长/标点三重约束 |

分词解决不了"这一行只能放 21 个字"的问题，也解决不了"这段话说太久了要断开"。

而且 ASR 输出的已经是 token 了（粒度差不多是词），**不需要再分词**。

**需要的是"按物理和阅读约束重新分组"，这是个独一无二的问题。**

---

## 9. 动手练习

**练习 1：观察不同 max_chars 的效果**

```bash
subgen.bat 测试.mp4 --max-chars 12 -o ./c12
subgen.bat 测试.mp4 --max-chars 21 -o ./c21
subgen.bat 测试.mp4 --max-chars 30 -o ./c30
```

打开三个 srt 对比：

- `c12`：条数多，每行短，像"快节奏"字幕
- `c21`：标准
- `c30`：条数少，每行长，接近满屏

**哪种适合你的素材，取决于画面构图和目标平台。**

**练习 2：测试行首标点规则**

直接调用折行函数：

```python
import sys; sys.path.insert(0, '.')
from subgen.segmenter import wrap_lines

text = "他说这里很危险，我们快走吧，不要回头。"
for w in (20, 24, 28):
    print(f"max_width={w}:")
    for line in wrap_lines(text, max_width=w):
        print(f"  |{line}|")
```

**检查每一行的开头有没有标点。** 应该都没有——这正是第三步的作用。

**练习 3：验证宽度计算**

```python
from subgen.segmenter import text_width

pairs = [
    ("你好世界", "Hello world"),
    ("这是一句话", "This is a sentence"),
]
for cn, en in pairs:
    print(f"{cn}: 字符 {len(cn)}, 宽度 {text_width(cn)}")
    print(f"{en}: 字符 {len(en)}, 宽度 {text_width(en)}")
```

你会发现：中文 4 个字宽度 8，英文 11 个字符宽度 11，
**视觉上确实差不多宽**。

**练习 4：拆掉一个约束看看**

把 `build_cues` 里的 `if s[-1] in SENT_END: flush()` 注释掉，
再跑一次。你会发现字幕变成了一长串**不会在句号处断开**的东西。

**这个练习的目的是让你体会：三个约束缺一不可。**

---

## 10. 小结

**三句话**：

1. **字幕不是转录**——ASR 给的是语义正确的长句，字幕要的是**可读**的短句
2. 折行用**打分制**：`score = 宽度 + 标点奖励`，40 分的句末标点奖励能压过任何宽度差异
3. `_post_process` 修的三件事（补时长、防闪烁、防重叠）都是**真实观感问题**，
   不做的话技术上能跑，但看片上就是别扭

**下一篇** → [06 · 第四层：字幕格式输出](06-formats.md)：切好的 cue 怎么写成 SR / VTT / ASS 五种格式。
