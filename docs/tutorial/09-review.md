# 09 · 校对工作台

> **本篇目标**：做一个能边听边改字幕的时间轴编辑器。
> **前置知识**：读完 05、06 篇；了解 HTML5 `<video>` 和 Canvas。
> **对应代码**：`subgen/review.py`（316 行）+ `subgen/waveform.py`（99 行）+ `webui/review.html`
> **耗时**：40 分钟。

---

## 1. 本篇要解决的问题

机器生成的字幕**一定会有错**。实测准确率大概 95%~98%，
意味着一集 40 分钟的片子有 20~80 处错误。

**必须有个人工修正的环节。**

要改字幕，光看文字不够——**你得听着原音改**。所以需要一个工具：

- 播放视频/音频
- 显示波形（看得见哪里有人说话）
- 显示字幕条目（可编辑）
- 改完能保存

**这就是「校对工作台」。**

---

## 2. 核心概念：波形为什么自己提

时间轴要画波形，让用户看得见"这句话在哪个位置"。

浏览器能直接从视频文件读采样吗？

```javascript
const audioCtx = new AudioContext();
const buffer = await audioCtx.decodeAudioData(arrayBuffer);
```

能。但**问题很大**：

| 方案 | 40 分钟视频的耗时 | 内存 |
|---|---|---|
| Web Audio 解码全文件 | **几十秒** | 几百 MB |
| **ffmpeg 降采样 + 算峰值** | **约 1 秒** | 几百 KB |

**差两个数量级。**

### 为什么差这么多

Web Audio 要**完整解码成 float32 PCM**：

```
40 分钟 × 60 秒 × 44100 Hz × 4 字节 × 2 声道
= 1.27 GB
```

而 ffmpeg 方案：

```bash
ffmpeg -i 视频.mp4 -ac 1 -ar 8000 -f s16le -
```

降采样到 **8 kHz 单声道**，只用来算"包络"（每个时间点的最大振幅），
**数据量降到 1/11**，而且可以流式处理。

### 更进一步：只算峰值

```python
PPS = 100              # 每秒 100 个采样点
SAMPLE_RATE = 8000     # 8 kHz 足够算包络
```

输出的是**每个点位一个字节**（0~255 的振幅）：

```
40 分钟 × 60 秒 × 100 点/秒 = 240,000 字节 = 234 KB
```

**从 1.27 GB 降到 234 KB。** 而且绘制时只需要画柱状图，非常快。

> 💡 **这就是"够用就好"的典型例子**：
> 我们不需要原始音频数据，只需要"哪里响、哪里静"。
> 降低精度换来两个数量级的收益。

### 分块提取

```python
def get_waveform(self, path="", start=0.0, duration=0.0) -> dict:
    """按需取某个时间段的波形。时间轴只显示可视范围。"""
```

因为时间轴可能显示 40 分钟全貌，也可能放大到 5 秒。
**不放大时不需要精度，放大时只取那一段。**

```python
# review.html 里的调用
const r = await window.pywebview.api.get_waveform(videoPath, start, dur);
```

---

## 3. 核心概念：媒体怎么给浏览器

界面是 HTML，但视频文件在本地磁盘。怎么让 `<video>` 播放？

### 方案对比

| 方案 | 问题 |
|---|---|
| `file:///C:/...` | 中文路径编码问题；某些 WebView2 版本禁止 |
| `blob:` URL | 要先读进内存，大文件受不了 |
| **本地 HTTP 服务** | ✅ 支持 Range 请求，可以拖动进度条 |

```python
class MediaServer:
    """把本地视频/音频以 HTTP 方式提供给界面播放。"""

    def url_for(self, path: str) -> str:
        """返回 http://127.0.0.1:PORT/media?path=... 形式的地址。"""
```

**关键：必须支持 Range 请求。**

`<video>` 拖动进度条时会发 `Range: bytes=1000000-`，
服务端要返回对应片段。不支持 Range 的话，**进度条拖不动**。

> 💡 本地回环（127.0.0.1）不出网，安全性没问题。
> 端口随机分配，避免冲突。

---

## 4. 代码走读：数据流

```
review.html（界面）
    │
    ├─ review_init()        → 拿到视频路径、字幕路径、时长
    ├─ get_waveform(...)    → 拿波形峰值数组
    ├─ load_subtitle(path)  → 拿 cues 列表
    │
    │  （用户在界面上编辑）
    │
    └─ save_subtitle(path, cues) → 写回文件
```

### `prepare()` —— 进入工作台前

```python
def prepare(video_path: str, sub_path: str = "") -> Dict[str, Any]:
    """准备数据：视频路径、字幕路径、时长、媒体 URL。"""
```

### `save_subtitle()` —— 保存

```python
def save_subtitle(self, path: str, cues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把界面上的 cues 写回字幕文件。

    注意：界面传来的 cue 可能改了时间、改了文字、增删了条目，
    要完整重建文件而不是增量修改。
    """
```

**为什么"完整重建"？**

因为界面上的操作是不可预测的：用户可能
- 合并两条
- 拆分一条
- 删除
- 调整时间

**增量更新会非常复杂且容易出错。整体重写最简单可靠。**

> 💡 这正是字幕文件的好处：**纯文本、体积小、整体重写成本极低。**
> 如果是数据库或二进制格式，就不能这么干了。

---

## 5. 核心概念：时间轴交互

这是界面部分最复杂的逻辑。几个关键点：

### 缩放与滚动

```
可视范围 = [viewStart, viewStart + viewDuration]
```

| 操作 | 效果 |
|---|---|
| 滚轮 | 以鼠标位置为中心缩放 |
| 拖动 | 平移 |
| 双击 | 重置为全览 |

**以鼠标位置为中心很重要**——否则缩放后找不到刚才看的地方。

### 波形绘制

```javascript
function drawWaveform() {
  const pps = canvas.width / viewDuration;   // 每像素多少秒
  ctx.beginPath();
  for (let px = 0; px < canvas.width; px++) {
    const t = viewStart + px / pps;
    const peak = peaks[Math.floor(t * PPS)] || 0;
    // 以中线为基准，上下画出振幅
    ctx.moveTo(px, mid - peak * scale);
    ctx.lineTo(px, mid + peak * scale);
  }
  ctx.stroke();
}
```

**只画可视范围内的点**，所以放大时不需要重算全部数据。

### 字幕块渲染

每条 cue 在时间轴上画一个矩形：

```javascript
cues.forEach(cue => {
  const x1 = (cue.start - viewStart) * pps;
  const x2 = (cue.end - viewStart) * pps;
  if (x2 < 0 || x1 > canvas.width) return;   // 不在可视范围，跳过
  ctx.fillRect(x1, y, x2 - x1, h);
});
```

**这个 `if` 很重要**——40 分钟可能有 400 条 cue，
但可视范围里可能只有 10 条。跳过不需要画的能省 97% 的时间。

---

## 6. 一个真实的设计取舍

**问题**：工作台要不要支持"重新识别单句"？

技术上可行：选中一条 cue，重新跑 ASR，替换结果。

**最后没做**，理由：

1. **成本高**：要重新加载模型、切片、推理，一次几秒
2. **收益低**：错的地方通常是识别错字，**直接改字比重新识别快**
3. **复杂度高**：要处理模型状态、异步、失败回滚

**最终做的是**：让编辑本身足够顺手——
键盘快捷键、波形对齐、时间微调按钮。

> 💡 **功能取舍的判断标准**：用户真正想要的是"改对"，
> 不是"用最先进的方式改对"。**最快达到目的的方式就是最好的方式。**

---

## 7. 界面的三个贴心设计

### ① 播放到哪，自动高亮哪条

```javascript
video.addEventListener('timeupdate', () => {
  const t = video.currentTime;
  const idx = cues.findIndex(c => t >= c.start && t <= c.end);
  if (idx !== -1) highlightCue(idx);
});
```

**编辑字幕时，视线不用在时间轴和列表之间来回找。**

### ② 点击时间轴跳转播放

```javascript
canvas.addEventListener('click', (e) => {
  const t = viewStart + (e.offsetX / canvas.width) * viewDuration;
  video.currentTime = t;
});
```

**听到哪里不对，点一下就跳过去改。**

### ③ 快捷键

| 键 | 作用 |
|---|---|
| `Space` | 播放/暂停 |
| `Tab` | 下一条 |
| `Shift+Tab` | 上一条 |
| `Ctrl+S` | 保存 |
| `←` / `→` | 微调时间 ±0.1s |

**校对是高频重复劳动，快捷键能省一半时间。**

---

## 8. 动手练习

**练习 1：观察波形提取的速度差异**

```python
import time
from subgen.waveform import extract_peaks

t0 = time.time()
peaks = extract_peaks("长视频.mp4")
print(f"提取耗时 {time.time()-t0:.2f}s，点数 {len(peaks)}")
```

对比一下如果用 Web Audio 解码要多久（在浏览器控制台试）。

**练习 2：验证 Range 请求**

启动界面后打开工作台，用浏览器开发者工具看网络请求：

```
GET /media?path=... HTTP/1.1
Range: bytes=0-
```

拖动进度条，观察后续请求的 `Range` 头。
**如果服务端不返回 `206 Partial Content`，进度条就拖不动。**

**练习 3：改一条字幕并保存**

在工作台里改一条，保存，然后用记事本打开 srt 确认改动生效。
**特别注意时间戳有没有被破坏。**

**练习 4：极端缩放**

把时间轴放大到 1 秒窗口，再缩到 40 分钟全览。
观察波形和字幕块是否都正常渲染（不会卡顿、不会错位）。

---

## 9. 小结

**三句话**：

1. **波形自己提**：ffmpeg 降采样算峰值，从 1.27 GB 降到 234 KB
2. **媒体走本地 HTTP**：必须支持 Range 请求，否则进度条拖不动
3. **保存时整体重写**：界面编辑不可预测，增量更新太复杂

**下一篇** → [10 · 任务编排与分层](10-pipeline.md)：进入工程化部分。
看 CLI 和 GUI 怎么共用同一套业务逻辑，避免"双份维护地狱"。
