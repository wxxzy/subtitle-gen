# 01 · 环境准备与跑通第一个字幕

> **本篇目标**：把环境装好，跑出一条字幕，建立「这东西真的能用」的体感。
> **前置知识**：会用命令行，知道 Python 虚拟环境是什么。
> **耗时**：20 分钟（首次装依赖 + 下载模型约 10 分钟）。

---

## 1. 先看要做什么

这篇不写代码，只做三件事：

1. 装依赖（Python 包 + ffmpeg）
2. 自检（确认显卡、ffmpeg、依赖都正常）
3. 跑一个视频，看输出

---

## 2. 检查前置条件

### Python 3.10 ~ 3.12

```bash
python --version
```

必须是 **3.10 及以上、3.12 及以下**。原因：

- 3.9 及以下：`faster-whisper` 的某些依赖不支持
- 3.13 及以上：部分 CUDA 相关的二进制包还没跟上

> 💡 **本项目的选择是 3.11**。理由是 Pascal 架构显卡（如 GTX 1080 Ti）
> 的 CUDA / PyTorch 生态对 3.11 兼容最成熟。如果你用的是 40 系显卡，
> 3.12 也没问题。

### ffmpeg

```bash
ffmpeg -version
```

没有的话，Windows 上最省事：

```bash
winget install --id Gyan.FFmpeg -e
```

> ⚠️ 装完**必须重开终端**，否则 PATH 还是旧的。

### 显卡（可选但强烈建议）

```bash
nvidia-smi
```

能出信息就说明有 N 卡。没有也能跑，只是会退回 CPU（慢 10 倍以上）。

---

## 3. 一键安装

Windows 用户直接双击：

```
install.bat
```

它做了四件事：

| 步骤 | 做什么 | 为什么 |
|---|---|---|
| 1 | 检查 python | 提前报错，比装到一半失败友好 |
| 2 | 检查 ffmpeg，缺失则 winget 装 | 少一步手动操作 |
| 3 | 创建 `.venv` 虚拟环境 | 不污染系统 Python |
| 4 | 装依赖 + 自检 | 用清华源加速 |

依赖装完约 **300 MB**（对比：如果用 `openai-whisper`，光 PyTorch 就 2.5 GB）。

**手动安装**（非 Windows 或想自己控制）：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### requirements.txt 里都是什么

```text
faster-whisper>=1.1.0        # 识别引擎
huggingface-hub>=0.23.0      # 下载模型（不用它做推理）
sentencepiece>=0.2.0         # 离线翻译的分词器
zhconv>=1.4.0                # 繁转简（中文识别后处理）
nvidia-cublas-cu12           # ┐
nvidia-cudnn-cu12            # ├ GPU 运行库，pip 装，免装 CUDA Toolkit
nvidia-cuda-runtime-cu12     # ┘
pywebview>=6.0               # 图形界面（可选）
pillow>=10.0.0               # 图像分析（去字幕定位、波形）
```

**注意 `nvidia-cublas-cu12` 这三个**——它们让 GPU 能用，但**不需要你装 CUDA Toolkit**。
这是本项目"依赖尽量少"思路的又一次体现。

---

## 4. 自检

```bash
.venv/Scripts/python.exe -m subgen.cli --check
```

输出大致是这样：

```
ffmpeg       : 6.1.1
Python       : 3.11.9
CUDA 显卡    : NVIDIA GeForce GTX 1080 Ti  ×1
计算能力     : CC 6.1
推荐精度     : int8
模型缓存     : C:\...\subgen\models  (5.9 GB)
```

**重点看两行**：

- **推荐精度**：这个值是自动算的。看到 `int8` 不要意外，这是**故意选的**——
  04 篇会详细解释为什么 Pascal 架构必须用 int8 而不是 float32/float16
- **模型缓存**：第一次是空的，跑第一个视频时会自动下载

### 如果 `--check` 报错

| 报错 | 原因 | 解决 |
|---|---|---|
| `未找到 ffmpeg` | PATH 没生效 | 重开终端 |
| `CUDA 显卡: 无` | 没装驱动 或 没装 `nvidia-*-cu12` | `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` |
| 导入错误 | 依赖没装全 | 重跑 `install.bat` |

---

## 5. 跑第一个字幕

准备一个短一点的视频（**1~2 分钟**，别一上来就 40 分钟），然后：

```bash
subgen.bat 你的视频.mp4
```

或者直接用 Python：

```bash
.venv/Scripts/python.exe -m subgen.cli 你的视频.mp4
```

### 第一次会发生什么

```
[模型] 首次使用，开始下载（约 1.5 GB，已走国内镜像）...
      [##########..........]  52%  3.2 MB/s  剩余约 3 分钟
[模型] 下载完成
[设备] 检测到 Pascal 架构显卡（CC 6.1）...
[1/3] 抽取音轨（16 kHz 单声道）…
      完成，用时 0.5s，音频 1.9 MB
[2/3] 识别中…
      [####################] 100%  62.2/62.2s
      完成，用时 4.1s（15.1x 实时）  语言 zh(100%)
[3/3] 智能断句并导出…
      生成 16 条字幕
      -> 你的视频.srt
```

**几个数字值得注意**：

- `15.1x 实时` —— 62 秒的音频，4.1 秒处理完
- `语言 zh(100%)` —— 自动检测出来的
- `生成 16 条字幕` —— 这里就是核心价值所在，16 条是**切好的、能直接上屏的**，
  不是 ASR 原始的 token 流

### 输出在哪

默认和源视频**同目录同名**，只换扩展名：

```
你的视频.mp4
你的视频.srt    ← 这里
```

---

## 6. 常用参数速查

跑通之后，这几个参数最常用：

```bash
# 指定输出目录
subgen.bat 视频.mp4 -o ./字幕

# 一次出多种格式
subgen.bat 视频.mp4 -f srt vtt ass

# 翻译成英文（离线模型）
subgen.bat 视频.mp4 -t en

# 翻译 + 中英双语
subgen.bat 视频.mp4 -t en --bilingual

# 烧录进画面（出成片）
subgen.bat 视频.mp4 -t en --burn

# 批量处理整个目录
subgen.bat ./剧集/*.mp4 -o ./字幕 -j 1
```

> ⚠️ `-j` 是并发数。**有显卡时建议 `-j 1`** —— 显存不够时并发会互相抢，
> 反而更慢甚至 OOM。CPU 上可以开到核数的一半。

---

## 7. 打开界面（可选）

```bash
启动界面.bat
```

第一次打开会看到主界面，左侧是设置面板，右侧是文件队列。

如果界面打不开，`启动界面.bat` 会自动切到诊断模式显示错误信息。

---

## 8. 动手练习

**练习 1：对比不同模型**

```bash
# 用 turbo（默认，快）
subgen.bat 视频.mp4 -o ./out_turbo

# 用 large（最准，慢）
subgen.bat 视频.mp4 -m large -o ./out_large
```

比较两个 `.srt` 的条数和内容差异。**同一条音频，不同的断句结果**——
这就是 05 篇要讲的东西。

**练习 2：观察时间轴**

用记事本打开生成的 `.srt`：

```
1
00:00:00,000 --> 00:00:02,600
电梯里年轻男子带双马尾的女友回家见父母。

2
00:00:02,600 --> 00:00:05,120
...
```

注意三点：

- 时间戳格式是 `时:分:秒,毫秒`
- **每条不超过 2 行**
- **每条时长在 1~7 秒之间**

这三个约束不是随便定的，05 篇会讲它们怎么来的。

**练习 3：故意制造错误**

```bash
subgen.bat 一个不存在.mp4
```

观察报错信息。好的报错应该告诉你「哪里错了」，而不是甩一堆堆栈。
项目里所有错误路径都尽量做到这一点。

---

## 9. 小结

**三句话**：

1. 依赖只有 300 MB，因为不用 PyTorch——这是关键设计
2. `--check` 里的「推荐精度 int8」不是降级，是**针对 Pascal 架构的正确选择**
3. 一条命令 `subgen.bat 视频.mp4` 就能出字幕，但这条命令背后有六层处理

**下一篇** → [02 · 整体架构与数据流](02-architecture.md)：建立起全局图，之后每篇都会在这个图上"点亮"一层。

---

## 附：环境变量（可选）

这些不是必填，但配一次长期省事：

| 变量 | 作用 |
|---|---|
| `SUBGEN_API_KEY` | 大模型翻译的 Key（07 篇） |
| `SUBGEN_API_BASE` | 大模型接口地址 |
| `HF_ENDPOINT` | HuggingFace 镜像（默认已用国内镜像） |

Windows 设置（永久）：

```powershell
[Environment]::SetEnvironmentVariable('SUBGEN_API_KEY', 'sk-xxx', 'User')
```

设置完**要重开终端**才生效。
