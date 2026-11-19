# 11 · 进度上报与跨线程通信

> **本篇目标**：把后台处理进度安全地推给网页界面。
> **前置知识**：了解线程、知道什么是消息循环。
> **对应代码**：`subgen/reporter.py`（301 行）+ `gui.py` 的 `_emit`
> **耗时**：45 分钟

---

## ⚠️ 先给结论

**这是整个项目踩坑最多的地方。**

我在这里遇到过三次"程序看起来卡死、但没有任何报错"的情况，
每次排查都要几个小时。

三个坑：

| # | 坑 | 现象 |
|---|---|---|
| 1 | `evaluate_js` 从后台线程调用 | 处理线程被活活卡死，界面不动 |
| 2 | `ExecuteScriptAsync` 跨线程 | **静默失败**，脚本没执行但不报错 |
| 3 | 心跳线程没停 | 任务结束了还在刷进度，状态栏永远"处理中" |

**如果你只读本篇的一节，读第 3 节和第 6 节。**

---

## 1. 本篇要解决的问题

场景：点击"开始生成"，后台开始处理 40 分钟的视频，
界面要实时显示：

```
正在识别中…  ▓▓▓▓▓▓░░░░  62.2/1200s  已用 4.1s  预计剩余 1分20秒
```

**问题**：处理跑在**后台线程**，界面跑在**UI 线程**。
两个线程不能直接互相调用。

```
  主线程（UI）              后台线程（处理）
  ┌──────────┐             ┌──────────┐
  │ 消息循环  │             │ 抽音频    │
  │ 渲染界面  │  ←─ ??? ─→  │ 识别      │
  │          │             │ 断句      │
  └──────────┘             └──────────┘
```

---

## 2. 坑一：`evaluate_js` 会卡死

pywebview 提供了 `window.evaluate_js(script)`，看起来正好。

```python
# ❌ 第一版就是这么写的
def _emit(self, js: str):
    self._window.evaluate_js(js)      # 在后台线程调用
```

**结果：点开始后界面完全不动，进度一直是 0。**

**原因**（读 pywebview 源码才知道）：

```python
# pywebview 内部实现（简化）
def evaluate_js(self, script):
    self._semaphore.acquire()          # ← 等 UI 线程处理
    self.Invoke(...)                   # ← 投递到 UI 线程
    ...
```

它是**同步阻塞**的：调用后会等 UI 线程执行完并返回结果。

但此时 UI 线程正忙于**消息循环**（处理窗口事件），
而这个循环**没空闲去处理新投递的脚本**——

```
后台线程：acquire() 等待 ... 等 ... 等 ...（永远等下去）
UI 线程：忙消息循环，不知道有人在等
```

**死锁。**

更糟的是：**异常被吞掉了，没有任何报错**。
你只看到"进度不动"，完全不知道原因。

---

## 3. 坑二：`ExecuteScriptAsync` 静默失败

第二版换了条路——直接调底层 WebView2 的 COM 接口：

```python
# ❌ 第二版
self._window.native.ExecuteScriptAsync(js)
```

**结果：不报错，但脚本没执行。**

**原因**：COM 组件有**线程亲和性**（thread affinity）——
必须在创建它的那个线程（UI 线程）调用。
从后台线程调用时，COM 会**静默失败**：

- 不抛异常
- 不返回错误
- 脚本就是没跑

> 💡 **这类"静默失败"是最难查的 bug**。
> 有异常你至少知道哪行错了；静默失败你只能靠对比现象猜。

---

## 4. 正确答案：事件队列 + 前端轮询

两版都失败后，回到最朴素的思路：

> **后台线程不要碰 UI，只往队列里放东西。
> 让 UI 线程自己来取。**

```python
class GuiApi:
    def __init__(self):
        self._events: List[Dict] = []
        self._ev_lock = threading.Lock()

    def _emit(self, js: str) -> None:
        """后台线程调用：只入队，不碰 UI。"""
        with self._ev_lock:
            self._events.append({"js": js})

    def poll_events(self) -> List[Dict]:
        """UI 线程调用（由网页发起）：取走累积的事件。"""
        with self._ev_lock:
            if not self._events:
                return []
            out = list(self._events)
            self._events.clear()
            return out
```

前端定时轮询：

```javascript
let pollTimer = setInterval(pollOnce, 150);    // 每 150ms 取一次

async function pollOnce() {
  const evts = await window.pywebview.api.poll_events();
  if (!evts || !evts.length) return;
  for (const e of evts) {
    // e.js 形如 "window.onProgress && window.onProgress({...})"
    (new Function(e.js))();
  }
}
```

### 为什么这样就安全了

| 环节 | 在哪个线程 | 安全性 |
|---|---|---|
| `_emit()` 写队列 | 后台线程 | ✅ 只操作 list + Lock |
| `poll_events()` 读队列 | **UI 线程**（pywebview 的 API 通道保证） | ✅ |
| `onProgress()` 操作 DOM | UI 线程 | ✅ |

**关键：`poll_events` 是网页通过 pywebview 的 API 通道调用的，
那时候天然就在 UI 线程上。**

### 顺带解决的两个问题

**① 任务启动太快导致事件丢失**

如果任务是"启动即完成"（比如只做一个 5 秒的视频），
前端可能还没准备好接收事件，任务就结束了。

**队列会缓存——前端第一次轮询时能拿到全部事件。**

**② 事件顺序**

队列是 FIFO，天然保证顺序。

---

## 5. 心跳机制：为什么需要

有些阶段**长时间没有细粒度进度**：

| 阶段 | 耗时 | 有进度吗 |
|---|---|---|
| 抽音频 | 0.5s | 不需要 |
| 加载模型 | 3~10s | ❌ 一次性操作 |
| 识别 | 4min | ✅ 按音频秒数 |
| 翻译（LLM） | 1~3min | ⚠️ 一批一批 |
| 导出 | 0.5s | 不需要 |

**加载模型那 10 秒，界面完全静默。** 用户会想：卡死了吗？

所以需要一个**心跳**：即使没有新进度，也定期重发当前状态。

```python
class CallbackReporter:
    def start_heartbeat(self) -> None:
        def loop():
            while not self._hb_stop.wait(self._hb_interval):   # 默认 1 秒
                if self._cancel.is_set():
                    continue
                if self._hb_ended or self._p.stage in ("done", "error"):
                    break                                       # ← 自愈
                if self._stage_t0:
                    self._p.stage_elapsed = time.time() - self._stage_t0
                self._emit(force=True)

        self._hb_thread = threading.Thread(target=loop, daemon=True)
        self._hb_thread.start()
```

**它在做什么？**

每秒把 `stage_elapsed`（本阶段已用时）更新后重新发一次。
前端就能显示：

```
正在加载识别模型（仍在处理，本阶段已用 8 秒）…
```

用户看到"数字在涨"，就知道没卡死。

---

## 6. 坑三：心跳停不下来（最隐蔽）

**这是我踩的第三个坑，也是最快被用户发现的。**

### 现象

用户反馈（附截图）：

> "我应该已经执行完成了，可是状态栏还是一直在提示处理"

截图里：
- 队列 3 个文件**全部是绿色已完成**
- 但状态栏还停在 "1/3 · 灶边女王 · 已用 47:39"
- 日志 **732 条**，全是"导出字幕文件…"

### 排查

查了一圈，发现关键事实：

```bash
$ grep file_done subgen/*.py
subgen/reporter.py:70:    def file_done(self, index, count, name, ok, msg=""): ...
subgen/reporter.py:129:   def file_done(...): ...
subgen/reporter.py:244:   def file_done(...): ...
```

**`file_done()` 只在 reporter.py 里定义，从未被调用！**

### 因果链

```
file_done() 没被调用
   → 心跳线程不知道任务结束了
   → 继续每秒推送 stage="export" 的进度
   → ① 覆盖 onJobDone 写的"全部完成"
   → ② 反复触发前端的阶段日志
```

**两个看起来毫不相关的现象，同一个根因。**

实测验证：

```
run_one 返回后等 5 秒 → 又收到 5 条事件，stage 全是 export  ✗
```

### 修复：三层防护

**第一层：正确调用**

```python
if res and res.ok:
    ok_count += 1
else:
    fail_count += 1

rep.file_done(i, total, name, bool(res and res.ok), ...)   # ← 补上
```

失败的路径也要调：

```python
except Exception as e:
    rep.file_done(i, total, name, False, err)    # ← 失败也要停心跳
    fail_count += 1
    continue
```

**第二层：`finally` 兜底**

```python
finally:
    # 无论正常结束、异常还是取消，都要停心跳
    try:
        rep = self._reporter
        if rep is not None:
            rep.finish()
    except Exception:
        pass
```

**第三层：心跳自愈**

```python
if self._hb_ended or self._p.stage in ("done", "error"):
    break        # 阶段已结束，自己退出
```

**即使将来又有人忘了调 `file_done`，心跳也会自己发现并停掉。**

> 💡 **这个"三层防护"的思路值得学**：
> 第一层是"做对"，第二层是"忘了也能兜住"，第三层是"机制自愈"。
> 对于**容易静默失效**的地方，多层防护是值得的。

---

## 7. 坑四：已用时间的基准

用户截图里还有一个可疑数字：**"已用 47:39"**。

任务实际只跑了几分钟。

### 原因

```python
# ❌ 错误：模块级的 _T0
_T0 = time.time()          # 这是模块被 import 的时刻！

class CallbackReporter:
    def stage(self, ...):
        self._p.elapsed = self._stage_t0 - _T0      # ← 减的是模块加载时刻
```

**所以"已用时间" = 程序启动至今。**

用户开着界面 47 分钟才点开始，第一次任务就显示"已用 47:39"。

### 修复

```python
class CallbackReporter:
    def __init__(self, ...):
        # 每个 reporter 实例自己记开始时刻
        self._t0 = time.time()

    def stage(self, ...):
        self._p.elapsed = self._stage_t0 - self._t0
```

> 💡 **教训**：模块级的时间戳几乎总是错的。
> 任何"从何时开始计算"的基准，都应该**由使用者决定**，而不是在 import 时固定。

---

## 8. 代码走读：取消机制

长时间任务必须能取消。

```python
@dataclass
class Progress:
    ...

class CallbackReporter:
    def __init__(self, ..., cancel_event=None):
        self._cancel = cancel_event or threading.Event()

    def check_cancel(self):
        """在耗时循环里定期调用；用户取消则抛 Cancelled。"""
        if self._cancel.is_set():
            raise Cancelled("用户取消")
```

业务代码里定期检查：

```python
def run_one(...):
    ...
    for i, tok in enumerate(tokens):
        if i % 100 == 0:
            rep.check_cancel()        # 每 100 个 token 检查一次
```

**为什么用异常而不是返回 False？**

因为取消可能发生在**很深的调用栈**里（比如 ASR 内部）。
用异常能一路冒泡到顶层，不用每层都写 `if cancelled: return`。

### 前端取消

```javascript
$('#btnCancel').onclick = async () => {
  await window.pywebview.api.cancel_job();
  // 界面立即反馈，不必等后台真正停下
  $('#stText').textContent = '正在取消…';
};
```

```python
def cancel_job(self):
    self._cancel.set()
    rep = self._reporter
    if rep:
        rep.cancel()
    return {"ok": True}
```

---

## 9. 完整的事件流

一次批量任务的事件顺序：

```
window.onLog({"识别模型已就绪…"})
window.onFileStart({"name": "a.mp4", "duration": 41.5})
window.onProgress({stage: "audio", ...})
window.onProgress({stage: "asr", current: 0, total: 41.5, ...})
window.onProgress({stage: "asr", current: 5.2, total: 41.5, ...})
... （每 120ms 一条）
window.onProgress({stage: "export", ...})
window.onFileDone({ok: true, cues: 287, outputs: [...]})
window.onFileStart({"name": "b.mp4", ...})
...
window.onJobDone({ok_count: 3, fail_count: 0, elapsed: 133.2})
```

**注意 `onProgress` 的频率**：

```python
def _emit(self, *, force=False):
    now = time.time()
    if not force and now - self._last_emit < 0.12:
        return          # 限流：最快 8 次/秒
    self._last_emit = now
    ...
```

**为什么要限流？**

进度更新可能每秒几百次（ASR 每处理一个 token 就调 `update`）。
不限流会：
- 事件队列爆掉
- 前端频繁重排，卡顿
- 网络/序列化开销

**8 次/秒肉眼看起来是连续的**，够用。

---

## 10. 动手练习

**练习 1：复现"心跳不停"的问题**

把 `gui.py` 里的 `rep.file_done(...)` 注释掉，跑一个短任务，观察：

- 状态栏是不是一直"处理中"？
- 日志是不是被同一个阶段刷屏？

**这个练习能让你记住这个坑一辈子。**

**练习 2：验证事件队列的必要性**

把 `_emit` 改成直接调 `evaluate_js`：

```python
def _emit(self, js):
    self._window.evaluate_js(js)     # ← 危险
```

跑一个任务，观察界面是不是完全不动。**注意：不会有任何报错。**

**练习 3：测限流的效果**

把 `0.12` 改成 `0`（不限流），跑一个长视频，观察：
- 日志面板是不是疯狂滚动
- 界面是不是变卡

**练习 4：检查时间基准**

```python
from subgen.reporter import CallbackReporter
rep = CallbackReporter(on_progress=lambda p: None)
rep.stage("asr", "识别中…")
print(f"elapsed = {rep._p.elapsed:.1f}s")     # 应该接近 0
```

如果这个数字很大（几百秒），说明用错了基准。

---

## 11. 小结

**三句话**：

1. **后台线程绝对不能碰 UI**——`evaluate_js` 会卡死，`ExecuteScriptAsync` 会静默失败；
   正确做法是**事件队列 + 前端轮询**
2. **心跳必须有，但必须能停**——漏调 `file_done()` 会让任务结束后还在刷进度，
   导致状态栏永远"处理中"、日志被刷屏
3. **模块级的时间戳几乎总是错的**——计时基准应该由使用者在恰当的时候设置

**下一篇** → [12 · GUI 架构](12-gui.md)：在事件队列的基础上，
看整个界面是怎么组织的。
