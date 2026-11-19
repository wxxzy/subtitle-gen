# 07 · 翻译引擎

> **本篇目标**：把中文字幕翻成英文，且**时间轴严格对齐**。
> **前置知识**：读完 06 篇。
> **对应代码**：`subgen/translator.py`（865 行，本项目最长的模块）
> **耗时**：40 分钟。

---

## 1. 本篇要解决的问题

翻译字幕看起来就是「把每句文本丢给翻译模型」，但有四个坑：

| 坑 | 后果 |
|---|---|
| 时间轴错位 | 译文整体偏移，完全没法看 |
| 长度膨胀 | 中译英变长 1.5~2 倍，撑出屏幕 |
| 专有名词被乱译 | "男主" → "master"，"妙脆角猫" → "marrow horn cat" |
| 批量丢句 | 一次翻 100 条，模型少返回几条，后面全错位 |

**这四个坑，只有第一个是"翻译问题"，其余三个都是"工程问题"。**

---

## 2. 核心概念：时间轴不可重建

这是最关键的设计决策。

### 错误做法：翻译后重新切分

```
原文 16 条 cue
   ↓ 翻译
16 段英文
   ↓ 重新按长度切分
22 条 cue（因为英文更长）
   ↓ ❌ 现在怎么分配时间？
```

**没法分配。** 原始的时间戳对应的是中文的语义边界，
英文的语义边界和中文不一样，硬套会错位。

### 正确做法：一条对一条

```
Cue(start=0.0, end=2.6, lines=["电梯里年轻男子"])
   ↓ 翻译 lines
Cue(start=0.0, end=2.6, lines=["电梯里年轻男子"],
    trans_lines=["A young man in the elevator"])
   ↑ 时间轴原样保留
```

**译文存在 `Cue` 上，不新建结构。**

```python
@dataclass
class Cue:
    start: float
    end: float
    lines: List[str] = ...           # 原文
    trans_lines: List[str] = ...     # 译文

    def copy_timing(self, lines: List[str]) -> "Cue":
        """用新文本复制一条 cue，保留时间轴。"""
        return Cue(start=self.start, end=self.end, lines=list(lines))
```

代码注释里写得很清楚：

> `trans_lines` 存译文，仅在翻译场景填充。之所以放在 Cue 上而不是单独建表，
> 是因为时间轴必须与原文严格一一对应——**两者拆开存储很容易错位**。

### 但长度膨胀怎么办？

英文字幕会比中文长。解决方案是**在时间轴内部重新折行**：

```python
# 翻译后
c.trans_lines = wrap_lines(translated_text, max_line_width, max_lines)
```

**关键：折行不改变 cue 的 start/end，只改变行内文字的排布。**

如果实在装不下（英文太长），就只能：
- 减少字号
- 或者接受更紧凑的排版

但**不能拆成两条 cue**，那样时间轴就乱了。

> 💡 **这条原则可以推广**：当两个数据必须严格对应时，
> **不要拆成两个结构再用索引关联**，而是合并到一个结构里。
> 索引关联是最容易出错的方式。

---

## 3. 核心概念：两个后端

翻译有两种实现，各有取舍：

| 后端 | 实现 | 优点 | 缺点 |
|---|---|---|---|
| **离线** | CTranslate2 直跑 Marian 模型 | 免费、无需网络、快 | 质量一般 |
| **LLM** | 任意 OpenAI 兼容接口 | 质量好、懂上下文 | 要花钱、要网络 |

### 为什么保留离线后端

即使有了大模型，离线后端仍然有价值：

1. **不用配置任何东西**——装完就能用
2. **完全离线**——敏感素材不出本机
3. **速度快**——每秒几十条，大模型要几秒一条
4. **成本为零**

对于"先看看效果"或"预算敏感"的场景，离线后端是合理的起点。

### 后端选择逻辑

```python
def make_translator(opts, *, verbose=True):
    """按 backend 选择实现；auto 时优先 LLM（若已配 Key），否则离线。"""
    if opts.backend == "offline":
        return OfflineTranslator(opts, verbose=verbose)
    if opts.backend == "llm":
        return LLMTranslator(opts, verbose=verbose)

    # auto：有 Key 才走 LLM
    llm = LLMTranslator(opts, verbose=verbose)
    if llm.available():
        if verbose:
            print("[翻译] 检测到 API Key，使用 LLM 后端（质量更高）")
        return llm
    return OfflineTranslator(opts, verbose=verbose)
```

**`available()` 这个方法的必要性：**

```python
def available(self) -> bool:
    _, key = self._resolve_api()
    return bool(key)
```

如果没有它，`auto` 模式就无法判断"用户到底配没配 Key"，
只能先试 LLM 再捕获异常——**用异常做流程控制，不优雅也不可靠**。

---

## 4. 核心概念：术语保护（本项目最巧的设计）

### 问题

机器翻译遇到专有名词会**强行意译**：

| 原文 | 机翻结果 | 应该是什么 |
|---|---|---|
| 男主 | master | the male lead |
| 妙脆角猫 | marrow horn cat | the cat |
| 林晚晴 | Lin Wanqing（还行） | 保持音译 |

Marian 这类模型没见过这些词，只能按字面意思猜。

### 直觉解法：翻完再替换

```
1. 翻译
2. 在译文里找"master"，替换成"the male lead"
```

**为什么不行？**

- 模型可能用别的词（master / man / male / hero）
- 同一个词在不同语境译法不同
- 英文里本来就有 "master"，会误伤

### 实际解法：占位符

```
第 1 步：把术语替换成稀有 token
   "男主走了进来" → "XTERM0X走了进来"

第 2 步：翻译
   "XTERM0X走了进来" → "XTERM0X walked in"

第 3 步：还原
   "XTERM0X walked in" → "the male lead walked in"
```

**为什么这样可行？**

因为 `XTERM0X` 是**模型完全不认识的 token**。面对不认识的字符串，
模型的行为是**原样保留**——它不会去"翻译"一个看不懂的东西。

代码注释里写得很清楚：

> 为什么用占位符而不是简单替换：Marian 这类模型对不认识的专名会强行
> 「意译」（妙脆角猫 -> marrow horn cat、男主 -> master）。
> 换成 `XTERM0X` 这种非常规 token 后模型只能原样保留，
> 位置和重复次数都正确（**实测验证**），译完再还原即可。

### 三个实现细节

**① 长词优先**

```python
# 长词优先，避免短词先匹配把长词切碎
for term in sorted(g, key=len, reverse=True):
    if term in text:
        text = text.replace(term, self._term_to_ph(term))
```

假设术语表里有 `男主` 和 `男主角`：

```
❌ 短词优先：
   "男主角" → "XTERM0X角"     ← 被切碎了

✅ 长词优先：
   "男主角" → "XTERM0X"       ← 完整匹配
```

**② 容错还原**

```python
for variant in (ph, ph.lower(), ph.upper(),
                ph.replace("XTERM", "XTERM ").strip()):
    if variant in text:
        text = text.replace(variant, target)
```

模型可能会：
- 改大小写：`xterm0x`
- 加空格：`XTERM 0X`
- 只保留部分：`TERM0X`

所以还原时要做多种变体匹配。

**③ 兜底清理**

```python
# 兜底：清掉任何残留的占位符碎片
text = re.sub(r"\bX\s?TERM\s?\d+\s?X\b", "", text, flags=re.I)
text = re.sub(r"\s{2,}", " ", text).strip()
```

万一有还原失败的，**宁可删掉也不能留在成品里**。
`XTERM0X` 出现在字幕里是灾难。

---

## 5. 核心概念：空行占位保证索引对齐

### 问题

批量翻译时，我们一次性发送 100 条：

```python
texts = ["第一句", "第二句", ..., "第一百句"]
results = translator.translate(texts)
```

如果模型返回 98 条（少了两条），怎么办？

```
texts[0]  ↔  results[0]   ✓
texts[50] ↔  results[49]  ❌ 从这条开始全错位
```

**这是最危险的 bug**——它不会报错，只是字幕内容整体错乱。

### 解法

```python
# 保留空行占位，保证索引对齐
```

具体做法：空文本不丢弃，用空字符串占位。

```python
# 假设第 3 条是空的
texts = ["第一句", "第二句", "", "第四句"]
results = translator.translate(texts)
# 空的那条不参与翻译，但占位保留
# results = ["First", "Second", "", "Fourth"]
```

**这样数组长度永远一致，索引永远对得上。**

### 补充：长度校验

```python
if len(out) != len(texts):
    # 补齐或截断，保证长度一致
```

即使有占位机制，还是要校验。因为模型可能：
- 擅自合并两条
- 返回带分隔符的文本

**防御性编程：永远不要相信外部 API 返回的数组长度。**

---

## 6. 代码走读：离线后端

```python
class OfflineTranslator(TranslatorBase):
    """基于 ctranslate2 直跑 Marian 模型（Argos / OPUS-MT 格式）。"""
```

用的是 **Argos Translate** 的模型（Marian 架构的 CTranslate2 版本）。

**为什么选 Argos 而不是别的？**

| 方案 | 依赖 | 说明 |
|---|---|---|
| `transformers` + Marian | PyTorch（2.5 GB） | 太重 |
| `argos-translate` | 依赖 `stanza` 等一堆 | 依赖链长 |
| **直接下 Argos 的 ct2 模型** | 只用 ctranslate2 | **最轻** |

**又是"依赖最小化"原则的体现**——直接拿模型文件，用已有的
`ctranslate2`（faster-whisper 已经带了）跑，不引入任何新依赖。

只需要额外装 `sentencepiece`（分词器，约 1 MB）。

### 加载逻辑

```python
def load(self) -> None:
    # 1. 找模型目录（缓存里有没有）
    # 2. 没有就下载（走镜像）
    # 3. 用 ctranslate2.Translator 加载
    self._translator = ctranslate2.Translator(model_dir, device="cpu")
```

注意：**翻译默认跑 CPU**。

原因：翻译的模型很小（几十 MB），CPU 就够快（每秒几十条）。
而 GPU 显存要留给 ASR——**显存是稀缺资源，要优先给最需要的地方**。

> 💡 这是个资源分配决策。1080 Ti 有 11 GB，ASR large 模型占 4.7 GB，
> 翻译只占几百 MB。但**同时占用会有峰值叠加**，可能 OOM。
> 让学生（翻译）用 CPU，把 GPU 留给主角（识别）。

---

## 7. 代码走读：LLM 后端

支持任意 OpenAI 兼容接口，内置 9 家预设（DeepSeek / 通义 / Kimi / 智谱 / ...）。

### 只用标准库

```python
def _chat(self, user_content: str) -> str:
    """调用 OpenAI 兼容的 chat/completions。

    用标准库 urllib 而不是 requests——只为发一个 POST 而多装一个依赖不划算，
    这也和「不引入 PyTorch」的整体轻量思路一致。
    """
    import urllib.request
    payload = json.dumps({
        "model": self.opts.llm_model,
        "messages": [...],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(f"{self._base}/chat/completions", ...)
```

**`temperature=0.2`** 而不是 0：完全确定性会让模型僵化，
0.2 保留一点多样性但基本稳定。翻译任务不需要创意。

### 错误处理分级

```python
except urllib.error.HTTPError as e:
    hint = ""
    if e.code == 401:
        hint = "Key 无效或已过期"
    elif e.code == 404:
        hint = "端点路径不对，检查是否漏了 /v1"
    elif e.code == 402:
        hint = "账户余额不足"
    elif e.code == 429:
        hint = "触发限流，稍后再试或换模型"
    return {"ok": False, "code": e.code, "error": ..., "hint": hint}
```

**把 HTTP 状态码翻译成可操作的建议。**

用户在界面上看到 "HTTP 404" 是懵的，看到"端点路径不对，检查是否漏了 /v1"
就知道该改什么了。

---

## 8. 翻译质量的两个杠杆

### 杠杆一：术语表（最有效）

```bash
subgen.bat 视频.mp4 -t en --mt-glossary "男主=the male lead; 女主=the female lead"
```

或者用文件：

```text
# glossary.txt
男主=the male lead
女主=the female lead
林晚晴=Lin Wanqing
顾言深=Gu Yanshen
```

**这是投入产出比最高的优化。** 花 5 分钟整理术语表，
能让专有名词的准确率从 30% 提到 95%。

### 杠杆二：上下文提示

```bash
subgen.bat 视频.mp4 -t en \
  --mt-context "现代都市短剧，婆媳矛盾题材，口语化，避免过度书面"
```

对 LLM 后端效果显著（它真的会调整语气），对离线后端无效（模型太小）。

---

## 9. 动手练习

**练习 1：对比两个后端**

```bash
# 离线
subgen.bat 测试.mp4 -t en --mt-backend offline -o ./off

# 大模型（需要配 Key）
subgen.bat 测试.mp4 -t en --mt-backend llm -o ./llm
```

对比译文质量，特别是：
- 口语是否自然
- 有没有"翻译腔"
- 上下文是否连贯

**练习 2：验证术语保护**

不加术语表跑一次，记下"男主"被译成什么。
然后加术语表再跑，对比差异。

```bash
subgen.bat 测试.mp4 -t en -o ./no_gloss
subgen.bat 测试.mp4 -t en --mt-glossary "男主=the male lead" -o ./with_gloss
```

**练习 3：检查时间轴是否对齐**

```bash
subgen.bat 测试.mp4 -t en --bilingual -o ./bi
```

打开 `.srt`，确认每一条的**中文和英文时间戳完全一致**。
这是最重要的验证——时间轴错了，后面全白搭。

**练习 4：制造长度膨胀**

故意翻译成德语（德语通常比英语更长）：

```bash
subgen.bat 测试.mp4 -t de --bilingual -o ./de
```

看折行算法怎么处理超长的德语。**这是对 06/07 两篇的综合检验。**

---

## 10. 小结

**三句话**：

1. **时间轴不能重建**——译文存在 `Cue.trans_lines` 上，一条对一条，绝不拆结构
2. **术语用占位符保护**：`XTERM0X` 是模型不认识的 token，它只能原样保留
3. 批量翻译必须**空行占位 + 长度校验**，否则几条丢失就会导致整体错位

**下一篇** → [08 · 去除视频自带字幕](08-desub.md)：有些素材本身带字幕，
不处理会导致新老字幕重叠。这一篇的结论**和直觉完全相反**。
