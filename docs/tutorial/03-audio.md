# 03 · 第一层：音视频处理

> **本篇目标**：理解为什么所有音视频操作都要经过 `audio.py` 这一个门。
> **前置知识**：知道 ffmpeg 是命令行工具。
> **对应代码**：`subgen/audio.py`（259 行）
> **耗时**：25 分钟。

---

## 1. 本篇要解决的问题

项目里到处都要碰音视频：

| 要做的事 | 用到的 ffmpeg 能力 |
|---|---|
| 抽音轨给 ASR | `-vn -ac 1 -ar 16000` |
| 读出视频时长 | `ffprobe -show_format` |
| 判断有没有音轨 | `ffprobe -show_streams` |
| 读出分辨率（算字号用） | `ffprobe -show_streams` |
| 把字幕烧进画面 | `-vf subtitles=...` |
| 把字幕封装成轨道 | `-c copy -metadata:s:s:0 language=...` |

如果每处都自己拼 ffmpeg 命令，会出什么问题？

- 参数写错（`-ss` 放 `-i` 前面还是后面，效果完全不同）
- 错误处理各写一遍，有的地方忘了检查 returncode
- 中文路径在 Windows 上各种编码问题
- 想换编码器时要改很多地方

**所以把这些全部收进一个类**：`FFmpeg`。

---

## 2. 核心概念：为什么是「16 kHz 单声道」

这是全篇最重要的一个数字。

### Whisper 的原生输入规格

```python
def extract_audio(self, src, dst=None, *, sample_rate: int = 16000,
                  mono: bool = True, ...):
    """抽音轨为 16 kHz 单声道 wav —— Whisper 的原生输入规格。"""
```

**为什么必须是这两个值？**

Whisper 模型内部第一步就是把音频转成 **80 维 Mel 频谱**，
而这一步固定按 **16 kHz 采样率、单声道** 的参数来做。

如果你给它是 44.1 kHz 立体声：

| 情况 | 结果 |
|---|---|
| ffmpeg 自动重采样 | 能跑，但你多付了一次全量解码 + 重采样 |
| 不重采样直接喂 | 音调错误、语速错误，识别结果彻底崩坏 |

**自己转，比让库转更可控**——而且 ffmpeg 的重采样质量比大多数库好。

### 顺带的三个好处

**1. 数据量小 20 倍**

```
44.1 kHz 立体声 16 bit → 每秒 176 KB
16 kHz   单声道 16 bit → 每秒  32 KB
```

40 分钟视频：`423 MB → 77 MB`。内存占用和 IO 都省。

**2. 去掉人声以外的信息**

单声道会合并左右声道。对于短剧这种「人声居中」的素材，这**顺便压掉了部分背景音**。

**3. `pcm_s16le` 是无损的**

```python
"-acodec", "pcm_s16le"
```

WAV 未压缩。有人会想"用 mp3 更小啊"——**不要**。
有损编码会在语音上引入伪影，ASR 对这类伪影很敏感。

> 💡 **通用原则**：给模型的输入，宁大勿损。中间环节的压缩往往会以精度为代价，
> 而这个代价在最终结果上很难归因。

---

## 3. 代码走读：`extract_audio`

```python
def extract_audio(self, src, dst=None, *, sample_rate=16000, mono=True,
                  denoise=False, start=None, duration=None) -> str:
    """抽音轨为 16 kHz 单声道 wav —— Whisper 的原生输入规格。"""
    if dst is None:
        fd, dst = tempfile.mkstemp(suffix=".wav", prefix="subgen_")
        os.close(fd)
```

**第一段：不传 `dst` 就自己建临时文件**。
注意 `os.close(fd)` —— `mkstemp` 返回的是文件描述符，
不关掉的话 Windows 上后面 ffmpeg 写这个文件会失败（文件被占用）。
> 这是 Windows 特有的坑，Linux 上不关也能跑。

```python
    cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src]
    if duration is not None:
        cmd += ["-t", str(duration)]
```

**第二段：拼命令**。

两个细节值得注意：

- **`-ss` 放在 `-i` 前面 = 快速定位**（关键帧跳转，不解码前面所有帧）；
  放在 `-i` 后面 = 精确但要解码全部。这里选了前者。
- **`-loglevel error`** —— 只输出错误。ffmpeg 默认会打一堆版权信息，
  GUI 里会把日志刷乱。

```python
    cmd += ["-vn", "-map", "0:a:0?", "-ac", "1" if mono else "2",
            "-ar", str(sample_rate), "-acodec", "pcm_s16le"]
```

**第三段：核心参数**。

| 参数 | 作用 | 为什么这么写 |
|---|---|---|
| `-vn` | 丢弃视频流 | 只要音频，省时间 |
| `-map 0:a:0?` | 取第一条音频流 | **末尾的 `?` 是关键**（见下） |
| `-ac 1` | 单声道 | Whisper 要求 |
| `-ar 16000` | 16 kHz | Whisper 要求 |
| `pcm_s16le` | 未压缩 WAV | 不引入编码伪影 |

**关于 `-map 0:a:0?` 的 `?`**

不带问号 `0:a:0`：如果文件没有音频流，ffmpeg **直接报错退出**。
带问号 `0:a:0?`：没有音频流时**映射为空但继续执行**，最后产出一个 0 字节文件。

那为什么还要带问号？

因为下游有检查：

```python
if r.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
    raise RuntimeError(...)
```

**这样错误信息由我们自己控制**，可以说"这个文件没有音轨"，
而不是把 ffmpeg 的原始报错甩给用户。

> 💡 **这个模式值得学**：让底层工具"安静地失败"，由上层判断并给出**人类可读**的错误。
> 用户不需要知道 `Stream map '0:a:0' matches no streams` 是什么意思。

```python
    if denoise:
        cmd += ["-af", "afftdn=nf=-25,dynaudnorm=f=200:g=5"]
```

**第四段：可选的降噪**。

两个滤镜串联：

- `afftdn=nf=-25` —— FFT 降噪，噪声底 -25 dB
- `dynaudnorm=f=200:g=5` —— 动态范围压缩，把小声的地方提上来

**默认关闭**。原因：短剧素材通常音频干净，降噪反而会损失细节。
这是给"录屏/实地录音"这类底噪大的素材准备的。

> ⚠️ 如果人声忽大忽小（比如远景戏），开 `--denoise` 有明显改善。

---

## 4. 错误处理的统一模式

整个 `FFmpeg` 类的每个方法都遵循同一个模式：

```python
r = subprocess.run(cmd, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
if r.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
    raise RuntimeError(f"抽取音频失败：{(r.stderr or '')[-500:]}")
```

**三重检查**：

| 检查 | 防的是什么 |
|---|---|
| `returncode != 0` | ffmpeg 报错 |
| `not os.path.isfile(dst)` | 文件没生成 |
| `os.path.getsize(dst) == 0` | 生成了但内容为空 |

**为什么要三重？** 因为真实遇到过「returncode=0 但产出 0 字节」的情况
（输入文件损坏时，ffmpeg 有时会"成功"退出）。

`errors="replace"` 也很关键——中文路径 / 中文报错在 Windows 上
解码可能出错，不加这个会抛 `UnicodeDecodeError`，把真正的错误掩盖掉。

---

## 5. 为什么不直接用 ffmpeg-python 这类库

有现成的封装库，比如 `ffmpeg-python`。为什么没用？

| 方案 | 优点 | 缺点 |
|---|---|---|
| `ffmpeg-python` | 链式 API 好看 | 多一个依赖；复杂滤镜仍要拼字符串；错误处理还是要自己做 |
| **直接 subprocess** | 零依赖；完全可控；出错时能看到原始命令 | 要自己拼参数 |

**决定性理由：出了问题能看见真实命令。**

用库封装时，如果某个参数拼错了，你很难知道最终执行的到底是什么命令。
直接 `subprocess.run(cmd)` 的话，把 `cmd` 打印出来就能复制到终端复现。

> 这个项目的整体倾向是：**能用标准库就不引入第三方依赖**。
> 全项目只有 8 个直接依赖，这是克制的结果。

---

## 6. 其他方法速览

```python
class FFmpeg:
    def probe(self, path) -> dict:        # ffprobe 原始输出
    def duration(self, path) -> float:    # 时长（秒）
    def has_audio(self, path) -> bool:    # 有没有音轨
    def video_size(self, path) -> (w,h):  # 分辨率，算字号用
    def extract_audio(...) -> str:        # 抽音频
    def burn(self, video, subtitle, out): # 烧录硬字幕
    def mux_soft(self, video, sub, out):  # 封装单条字幕轨
    def mux_multi(self, video, tracks, out) # 封装多条字幕轨
```

### `burn` 里藏着一个大坑

```python
def burn(self, video, subtitle, out, *, font=None):
    # Windows 下 subtitles 滤镜的路径必须特殊转义
```

ffmpeg 的 `subtitles=` 滤镜对 Windows 路径非常挑剔：

```
❌ subtitles=C:\Users\a\sub.srt          → 冒号被当成参数分隔
❌ subtitles=C:/Users/a/sub.srt          → 反斜杠问题
✅ subtitles=filename='C\:/Users/a/sub.srt'  → 正确写法
```

**而且路径里有中文、空格、单引号时还要再转义一层。**

这也是为什么要封装——这种转义规则散落在各处的话，迟早有一个地方写错。

---

## 7. 动手练习

**练习 1：观察不同采样率的差异**

```bash
# 正常抽
ffmpeg -i 测试.mp4 -vn -ac 1 -ar 16000 -acodec pcm_s16le a_16k.wav

# 高采样率抽
ffmpeg -i 测试.mp4 -vn -ac 1 -ar 44100 -acodec pcm_s16le a_44k.wav

ls -lh a_16k.wav a_44k.wav
```

看体积差多少，然后**用 44.1k 的跑一次识别**，对比速度和结果。
你会发现 Whisper 内部还是会转成 16k，多花的时间纯属浪费。

**练习 2：验证 `-ss` 位置的影响**

```bash
# 快速定位（解码前跳转）
time ffmpeg -ss 600 -i 长视频.mp4 -t 10 -vn out1.wav

# 精确解码（解码后跳转）
time ffmpeg -ss 600 -i 长视频.mp4 -t 10 -vn -accurate_seek out2.wav
```

在 40 分钟的视频上，第一个明显更快。

**练习 3：制造"有视频没音频"的文件**

```bash
ffmpeg -f lavfi -i testsrc=duration=5:size=320x240:rate=30 -an novideoaudio.mp4
subgen.bat novideoaudio.mp4
```

看它给出的报错是不是人类可读的"该文件没有音轨"，
而不是 ffmpeg 的原始错误。

---

## 8. 小结

**三句话**：

1. **16 kHz 单声道是 Whisper 的原生规格**，自己转比让库转更可控
2. `pcm_s16le` 不压缩 —— 给模型的输入宁大勿损
3. 每个方法都做**三重检查**（returncode / 文件存在 / 非空），
   因为真实遇到过"成功退出但产出空文件"

**下一篇** → [04 · 第二层：语音识别](04-asr.md)：音频有了，怎么把它变成带时间戳的词。
这一篇里有个 **Pascal 架构的精度陷阱**，坑了我很久。
