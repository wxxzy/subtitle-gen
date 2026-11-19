# 06 · 第四层：字幕格式输出

> **本篇目标**：把 `List[Cue]` 写成 SRT / VTT / ASS / LRC / TXT 五种格式。
> **前置知识**：读完 05 篇。
> **对应代码**：`subgen/formats.py`（243 行）
> **耗时**：25 分钟。

---

## 1. 本篇要解决的问题

`Cue` 是我们自己的数据结构，但**播放器不认**。要转成标准格式。

市面上主流字幕格式有五种，各有各的用途：

| 格式 | 用在哪 | 特点 |
|---|---|---|
| **SRT** | 最通用 | 纯文本，人人都认，但不支持样式 |
| **VTT** | 网页 `<video>` | HTML5 标准，支持基础样式 |
| **ASS** | 影视压制 | 支持字体、位置、特效，功能最强 |
| **LRC** | 音乐播放器 | 一行一句，带时间标签 |
| **TXT** | 纯文稿 | 无时间轴，给人看的 |

**看起来只是"换个格式输出"，但有几个真实陷阱。**

---

## 2. 陷阱一：时间格式不一样

这是最容易出错的地方——**三种格式的时间精度不同**。

```python
def fmt_ts(seconds: float, *, sep: str = ",", with_hours: bool = True) -> str:
    """秒 -> 00:00:00,000 形式。"""
    ms = int(round(seconds * 1000))     # 毫秒
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"
```

```python
def fmt_ass_ts(seconds: float) -> str:
    """ASS 用 0:00:00.00（百分秒）。"""
    cs = int(round(seconds * 100))      # 厘秒（1/100 秒）
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"
```

**对比**：

| 格式 | 精度 | 分隔符 | 小时 | 例子 |
|---|---|---|---|---|
| SRT | 毫秒（3 位） | **逗号** `,` | 必须 | `00:00:01,500` |
| VTT | 毫秒（3 位） | **点** `.` | 必须 | `00:00:01.500` |
| ASS | **厘秒**（2 位） | **点** `.` | **可省略** | `0:00:01.50` |

**三个坑：**

**① SRT 用逗号，VTT 用点**

这是历史遗留。很多人写 SRT 时用了点，导致某些播放器不认。

代码里用 `sep` 参数统一处理：

```python
def to_srt(cues, *, sep: str = ",") -> str:
def to_vtt(cues) -> str:
    return to_srt(cues, sep=".")      # ← 只改分隔符
```

**② ASS 是厘秒，不是毫秒**

`00:00:01.50` = 1.5 秒。如果你按毫秒写成 `00:00:01.500`，
ASS 解析器会读成 `1.500` 厘秒 = 1.005 秒，**全片错位**。

**③ ASS 的小时位不补零**

`0:00:01.50`（不是 `00:00:01.50`）。注意 `f"{h:d}"` 用的是 `d` 不是 `02d`。

> 💡 **这个细节容易被忽略**，因为大多数播放器宽容，但严格的压制工具会报错。

---

## 3. 陷阱二：双语字幕的顺序

```python
def _cue_body(c: Cue) -> str:
    """单语/双语共用的文本块。

    双语时原文在上译文在下——这是行业惯例（观众视线先落在母语）。
    """
```

**为什么原文在上？**

因为观众的母语是原文。阅读时视线先扫到第一行，
**母语在上能更快理解**，外语在下作为辅助。

如果反过来（外语在上），观众要先读外语、卡住、再看母语——体验很差。

**这个约定不是随便定的**，几乎所有正规字幕组都遵守。

```python
def _cue_body(c: Cue) -> str:
    lines = list(c.lines)
    if c.trans_lines:
        lines = lines + list(c.trans_lines)    # 原文在前，译文在后
    return "\n".join(lines)
```

---

## 4. 陷阱三：ASS 需要头部和样式

SRT 和 VTT 都是"纯列表"，ASS 则是个**带配置文件的格式**：

```python
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, ...
Style: Default,{font},{size},&H00FFFFFF,...

[Events]
Format: Layer, Start, End, Style, Text
"""
```

**关键点：`PlayResX` / `PlayResY` 必须和视频分辨率一致。**

```python
def to_ass(cues, *, width=1920, height=1080, font="Microsoft YaHei", fontsize=0):
```

因为 ASS 里的字号、位置都是**相对于这个坐标系的绝对像素值**。

如果视频是 1080×1920（竖屏），但 ASS 里写 `PlayResX: 1920`，
字幕会被拉伸变形。

**这就是为什么 `tasks.py` 里要先探测视频分辨率**：

```python
frame_w, frame_h = ff.video_size(work_src) if is_video(work_src) else (1920, 1080)
```

### 字号也要自适应

```python
def line_width_for(max_chars: int, w: int, h: int) -> int:
    """按画面实际宽度决定每行能放多少半角字符。

    max_chars 是「汉字数」，竖屏视频屏宽有限，直接沿用会溢出。
    取用户设定与画面物理容量的较小值——画面优先。
    """
    user_w = max(6, max_chars) * 2
    if w <= 0:
        return user_w
    usable = w * 0.84                    # 留边距
    fs = min(h * 0.045, usable / 20) if h > w else h * 0.05
    fs = max(14, fs)
    ...
```

**逻辑**：用户设的"每行 21 字"在横屏 1920 宽下没问题，
但在竖屏 720 宽下会溢出。所以取**用户设定**和**物理容量**的较小值。

> 💡 **原则：画面优先**。用户可以随便设参数，但最终不能让字幕超出画面。

---

## 5. 代码走读：五种格式

### SRT（最通用）

```python
def to_srt(cues, *, sep=",") -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))                              # 序号
        out.append(f"{fmt_ts(c.start, sep=sep)} --> {fmt_ts(c.end, sep=sep)}")
        out.append(_cue_body(c))
        out.append("")                                  # 空行分隔
    return "\n".join(out)
```

输出：

```
1
00:00:00,000 --> 00:00:02,600
电梯里年轻男子带双马尾的女友回家见父母。

2
00:00:02,600 --> 00:00:05,120
两人表情紧张，似乎有什么难言之隐。
```

**注意末尾的空行**——SRT 靠空行分隔条目，最后一条也要有。

### VTT

```python
def to_vtt(cues) -> str:
    return "WEBVTT\n\n" + to_srt(cues, sep=".")
```

**就这么简单**。区别只有两点：

1. 开头必须有 `WEBVTT`
2. 时间用点不用逗号

> 💡 这种"格式 A 是格式 B 的小变体"的情况，**复用比重写可靠**。
> 重写容易漏掉某个细节。

### ASS

```python
def to_ass(cues, *, width=1920, height=1080, font="Microsoft YaHei", fontsize=0):
    # 头部（含分辨率、字体、样式）
    header = ASS_HEADER.format(width=width, height=height, ...)
    lines = [header]
    for c in cues:
        text = "\\N".join(c.lines)          # ← 注意是 \N 不是 \n
        lines.append(
            f"Dialogue: 0,{fmt_ass_ts(c.start)},{fmt_ass_ts(c.end)},Default,,0,0,0,,{text}"
        )
    return "\n".join(lines)
```

**关键细节：换行用 `\N`**

ASS 里 `\N` 表示强制换行，`\n` 表示软换行（可能被忽略）。
**必须用 `\N`**，否则双语字幕会挤成一行。

### LRC（歌词格式）

```python
def to_lrc(cues, *, offset_ms: int = 0) -> str:
```

格式是 `[mm:ss.xx]歌词`，一行一句，**没有结束时间**。

```python
def to_txt(cues, *, with_ts=False, paragraphs=True) -> str:
```

纯文本。`paragraphs=True` 会把连续的字幕合并成段落——
适合做成"文稿"给人读，或者喂给翻译工具。

---

## 6. 一个真实的取舍

**问题**：ASS 支持 `四` 种对齐方式、十几种特效，
为什么这里只输出最简单的 `Default` 样式？

**理由**：

1. **我们的目标是"能用"，不是"好看"**
   字幕位置、描边、阴影这些，交给用户后期在剪辑软件里调更合适
2. **样式越复杂，跨平台兼容性越差**
   同一个 ASS 文件，不同播放器渲染结果可能差很多
3. **大部分平台（抖音/视频号）根本不吃 ASS**
   它们只要能烧进画面的硬字幕

所以 ASS 输出的定位是：**给需要精细控制的用户一个起点**，
而不是替代专业字幕软件。

> 💡 **判断标准**：一个功能如果要做到"好用"需要大量工作，
> 而"够用"只需要少量工作，且用户有更好的替代方案——**就做够用的版本**。

---

## 7. 动手练习

**练习 1：验证时间格式差异**

```python
import sys; sys.path.insert(0, '.')
from subgen.formats import fmt_ts, fmt_ass_ts

t = 3661.5      # 1 小时 1 分 1.5 秒
print("SRT :", fmt_ts(t))
print("VTT :", fmt_ts(t, sep="."))
print("ASS :", fmt_ass_ts(t))
```

预期输出：

```
SRT : 01:01:01,500
VTT : 01:01:01.500
ASS : 1:01:01.50      ← 注意小时不补零、只有 2 位小数
```

**练习 2：一次出全格式**

```bash
subgen.bat 测试.mp4 -f srt vtt ass lrc txt -o ./all
```

打开五个文件对比。特别看：

- `.ass` 的头部
- `.lrc` 的时间标签格式
- `.txt` 有没有多余的空行

**练习 3：检查双语顺序**

```bash
subgen.bat 测试.mp4 -t en --bilingual -f srt ass -o ./bi
```

打开 `.srt`，确认**中文在上、英文在下**。

**练习 4：故意用错精度**

手动把 ASS 的时间改成毫秒（3 位）：

```
Dialogue: 0,0:00:01.500,0:00:04.000,Default,,0,0,0,,测试
```

用播放器打开，看字幕是不是提前了。**这就是精度错误的后果。**

---

## 8. 小结

**三句话**：

1. 三种格式的时间精度不同：SRT/VTT 是毫秒，**ASS 是厘秒**——写错会全片错位
2. 双语字幕**原文在上、译文在下**是行业惯例，因为观众视线先落在母语
3. ASS 的 `PlayResX/Y` 必须等于视频分辨率，否则字幕会被拉伸

**下一篇** → [07 · 翻译引擎](07-translation.md)：字幕有了，怎么翻成英文——
而且**时间轴必须严格对齐**，这才是真正的难点。
