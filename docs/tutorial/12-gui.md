# 12 · GUI 架构

> **本篇目标**：理解「原生窗口 + 网页界面」这套架构，以及一个隐蔽的缓存陷阱。
> **前置知识**：读完 11 篇；会写基础 JS。
> **对应代码**：`subgen/gui.py`（808 行）+ `gui_main.py`（104 行）+ `webui/index.html`（3249 行）
> **耗时**：35 分钟。

---

## 1. 本篇要解决的问题

命令行够用了，为什么还要做界面？

**因为目标用户不是开发者。**

| 场景 | CLI 体验 | GUI 体验 |
|---|---|---|
| 拖入 3 个视频 | 敲路径 / 通配符 | 直接拖进去 |
| 看进度 | 滚动终端文字 | 进度条 + 状态栏 |
| 改参数 | 查 `--help` | 勾选/下拉 |
| 检查结果 | 去文件夹找 | 列表里点开 |

**界面不是为了好看，是为了降低使用门槛。**

---

## 2. 核心概念：为什么是 pywebview

有几种做桌面界面的方式：

| 方案 | 体积 | 开发方式 | 评价 |
|---|---|---|---|
| Electron | 150 MB+ | HTML/JS | 太重，拖个 Chromium |
| Tkinter | 自带 | Python | 界面古老，做复杂布局很痛苦 |
| PyQt | 60 MB | Python | 学曲线陡，许可证要注意 |
| **pywebview** | **约 1 MB** | **HTML/JS** | ✅ 用系统自带 WebView2 |

### pywebview 的原理

```
┌─────────────────────────────────┐
│  原生窗口（WinForms）             │
│  ┌───────────────────────────┐   │
│  │  WebView2（系统自带）      │   │
│  │  ┌─────────────────────┐  │   │
│  │  │  你的 HTML 界面      │  │   │
│  │  └─────────────────────┘  │   │
│  └───────────────────────────┘   │
└─────────────────────────────────┘
```

**关键：不打包浏览器内核，用系统已有的 WebView2。**

Windows 10/11 自带 WebView2 Runtime，所以：
- 安装包小（1 MB vs Electron 的 150 MB）
- 不用维护 Chromium 版本
- 界面用 Web 技术写，开发效率高

---

## 3. 核心概念：Python ↔ JS 双向通信

pywebview 提供两条通道：

### 方向一：JS 调 Python

```python
class GuiApi:
    def pick_files(self) -> List[str]:
        """弹出文件选择框，返回选中的路径。"""

    def start_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """启动任务。"""
```

```javascript
// JS 侧：像调本地函数一样
const files = await window.pywebview.api.pick_files();
const r = await window.pywebview.api.start_job({paths, options});
```

**注意：`js_api=api` 是构造参数。**

```python
window = webview.create_window(
    "subgen · 字幕生成",
    versioned_url(HTML_FILE),
    js_api=api,                # ← 把 GuiApi 实例暴露给前端
    width=1200, height=800,
)
```

**只有 `GuiApi` 的公开方法能从前端调**，其他内部方法（`_run_job` 等）不可见。

### 方向二：Python 推给 JS

**这条通道就是 11 篇讲的"事件队列 + 轮询"。**

```python
# Python 侧：入队
self._emit(f"window.onProgress && window.onProgress({json.dumps(data)})")
```

```javascript
// JS 侧：定时取
setInterval(async () => {
  const evts = await window.pywebview.api.poll_events();
  for (const e of evts) (new Function(e.js))();
}, 150);
```

**为什么要拼成 `"window.onProgress && ..."` 这种字符串？**

因为 `_emit` 只负责传递，不知道前端有没有定义这个函数。
加个 `&&` 判断，**即使某个版本的前端没实现这个回调，也不会报错**。

> 💡 这是一种**防御性设计**：让新旧版本的前后端能兼容。

---

## 4. API 设计的分类

`GuiApi` 的 20 个方法可以分成四类：

### ① 环境与配置

```python
get_env()              # ffmpeg 是否就绪、显卡型号、版本
load_settings()        # 读设置
save_settings(s)       # 写设置
list_llm_providers()   # 大模型服务商列表
```

### ② 文件操作

```python
pick_files()           # 选文件
pick_folder()          # 选目录
list_media(folder)     # 列目录下的媒体文件
resolve_dropped(names) # 解析拖放的文件名
reveal(path)           # 在资源管理器里显示
```

### ③ 任务控制

```python
start_job(payload)     # 启动
cancel_job()           # 取消
poll_events()          # 拉事件
probe(paths)           # 探测时长/体积（列表展示用）
```

### ④ 辅助功能

```python
detect_subtitle(path)  # 检测字幕位置
preview_desub(...)     # 去字幕预览图
test_llm(...)          # 测试大模型连接
open_review(...)       # 打开校对工作台
```

**这个分类本身就是一种设计**：每类方法职责清晰，前端调用时一目了然。

---

## 5. 核心概念：设置持久化

```python
SETTINGS_FILE = os.path.join(APP_DIR, ".gui_settings.json")

def load_settings(self):
    try:
        if os.path.isfile(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_settings(self, s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s or {}, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

**为什么存成文件而不是用浏览器的 localStorage？**

这是 11 篇那个"缓存坑"带来的教训：

> WebView2 的 `localStorage` 在 `private_mode=True` 时会丢失，
> 在 `private_mode=False` 时又会带来缓存问题。

**存到文件里，完全不依赖浏览器存储。** 换 WebView 版本、清缓存都不影响。

> 💡 **原则**：重要的状态不要存在**你不完全控制的存储**里。

---

## 6. 坑：WebView2 的强缓存

**这是用户主动反馈才发现的 bug。**

### 现象

> "通过启动界面.bat启动的GUI不是最新的？还需要编译吗"

用户看到的是**几天前的旧界面**，新加的功能完全没有。

### 排查

**第一步：确认要不要编译**

```bash
cat 启动界面.bat
# start "" ".venv\Scripts\pythonw.exe" -m subgen.gui_main
```

**直接跑源码，不需要编译。**

**第二步：确认磁盘文件是新的**

```python
len(open("webui/index.html").read())     # 73550 字符
"sProvider" in html                       # True
```

**磁盘文件是新的。**

**第三步：真实启动，回读渲染后的 DOM**

```python
win = webview.create_window("probe", HTML_FILE, js_api=api)
time.sleep(4)
r = win.evaluate_js("!!document.querySelector('#sProvider')")
# r == False      ← 渲染出来的界面里没有这个元素！
```

**磁盘 73550 字符，渲染 66235 字符**——加载的是旧版。

**第四步：对比不同启动方式**

| 启动方式 | 渲染结果 |
|---|---|
| `private_mode=False`（当时用的） | ❌ 旧版 66235 |
| `private_mode=True` | ✅ 新版 73538 |
| `http_server=False` | ✅ 新版 |

**第五步：决定性对照**

同一份文件、同样参数，只改 URL：

```
不带版本号      → {sProvider: false, len: 66235}   ❌
带 ?v=修改时间  → {sProvider: true,  len: 73538}   ✅
```

**根因：WebView2 按 URL 强缓存了旧页面，无视 `no-cache` 头，且永不过期。**

### 修复

```python
def versioned_url(path: str) -> str:
    """给页面 URL 加一个基于文件修改时间的版本号。

    ★ 为什么必须这么做 ★
    WebView2 会对本地 http 服务返回的页面做**强缓存**，且不按 pywebview 发的
    no-cache 头办事。结果是：**界面文件更新了，重启程序看到的还是旧界面**，
    连清浏览器缓存都未必生效，用户会以为"改了没生效"或"需要编译"。
    """
    try:
        v = int(os.path.getmtime(path))
    except OSError:
        return path
    return f"{path}?v={v}"
```

**加版本号后 URL 随文件变化，缓存自然失效。**

同时把 `private_mode` 改成 `True` 做双保险（配置存文件里，不受影响）。

> 💡 **这个坑的教训**：
> 1. "改了没生效"要用**运行时回读状态**验证，不能只看磁盘文件
> 2. 凡是 pywebview 应用，**给页面 URL 加版本号应该是标配**

---

## 7. 界面组织：单页应用

`webui/index.html` 是 3249 行，包含 HTML + CSS + JS。

**没有构建步骤，没有框架。**

### 结构

```
┌──────────────────────────────────────────┐
│ 顶栏（环境状态、版本）                      │
├──────────────┬───────────────────────────┤
│ 左：设置面板   │ 右：文件队列                │
│              │                           │
│ · 模型选择    │  ┌───────────────────┐    │
│ · 语言       │  │ file1.mp4  待处理  │    │
│ · 翻译       │  │ file2.mp4  ✓完成   │    │
│ · 排版       │  └───────────────────┘    │
│ · 成片       │                           │
│ · 原字幕处理  │                           │
│ · 大模型配置  │                           │
├──────────────┴───────────────────────────┤
│ 状态栏（阶段、进度条、已用时间）             │
├──────────────────────────────────────────┤
│ 日志面板（可折叠）                          │
└──────────────────────────────────────────┘
```

### 状态管理

```javascript
const S = {
  queue: [],        // 待处理文件列表
  running: false,   // 是否在跑
  outputs: [],      // 产出文件
  preview: [],      // 字幕预览
};
```

**一个全局对象管全部状态。** 没有 Redux、没有响应式框架。

**为什么够用？**

因为界面状态很简单：一个列表 + 一个运行标志 + 一些配置。
**引入状态管理框架是过度设计。**

> 💡 **判断标准**：如果状态可以装进一个对象、更新路径清晰可数，
> 就不需要框架。框架解决的是"状态复杂到你自己都记不清"的问题。

### 设置读写

```javascript
const SET_KEYS = ['sModel','sLang','sTr','sTgt','sBi','sApiKey',...];

function collectSettings() {
  return {
    model: $('#sModel').value,
    language: $('#sLang').value,
    translate: $('#sTr').checked ? $('#sTgt').value : null,
    ...
  };
}

function applySettings(s) {
  set('sModel', s.model);
  set('sLang', s.language);
  chk('sTr', !!s.translate);
  ...
}
```

**`SET_KEYS` 自动绑定**：遍历 id 列表，自动挂 `change` 事件触发保存。

---

## 8. 拖放：一个 pywebview 的缺口

HTML5 的拖放 API 能拿到 `File` 对象，但**拿不到真实路径**：

```javascript
dropZone.addEventListener('drop', (e) => {
  const file = e.dataTransfer.files[0];
  console.log(file.path);    // undefined（浏览器安全限制）
});
```

没有真实路径就**没法处理文件**。

### 解法：挂 WinForms 的拖放事件

```python
def attach_drop(window, on_dropped):
    """给原生窗口挂 DragDrop 事件。

    pywebview 不暴露拖放接口，只能下沉到 WinForms 层。
    """
    window.events.loaded += lambda: _hook(window, on_dropped)
```

拿到路径后推给前端：

```python
def push_dropped(self, paths):
    self._emit(f"window.onDropped && window.onDropped({json.dumps(paths)})")
```

**这是「用 Web 技术做桌面应用」的典型妥协**——
有些能力必须下沉到原生层。

> 💡 遇到这类缺口时，**先确认是不是真的做不到**，
> 再考虑"下沉到原生"。不要一上来就绕路。

---

## 9. 启动入口

```python
# gui_main.py
def main() -> int:
    if not os.path.isfile(HTML_FILE):
        return _fail(f"界面文件不存在：{HTML_FILE}")

    try:
        import webview
    except ImportError:
        return _fail("未安装 pywebview。\n请执行：pip install pywebview")

    from .gui import GuiApi, set_window
    from .dropbridge import attach as attach_drop

    api = GuiApi()
    window = webview.create_window(
        "subgen · 字幕生成",
        versioned_url(HTML_FILE),      # ← 版本号
        js_api=api,
        width=1200, height=800,
        min_size=(980, 660),
        text_select=False,
        confirm_close=False,
    )
    set_window(window)
    attach_drop(window, api.push_dropped)

    try:
        webview.start(debug=False, http_server=True, private_mode=True)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-800:]}")
    return 0
```

### `_fail()` 的设计

```python
def _fail(msg: str) -> int:
    """启动失败时给出可操作的提示，而不是丢一堆堆栈。"""
    print("启动界面失败", file=sys.stderr)
    print("排查建议：")
    print("  1. 确认已安装界面依赖：pip install pywebview")
    print("  2. 确认系统有 WebView2 运行时（Win10/11 通常自带）")
    print("  3. 也可以继续用命令行：subgen.bat 视频.mp4")
    return 1
```

**界面起不来时，用户看到的是"该装什么"而不是 traceback。**

而且 `启动界面.bat` 会检测启动失败，自动切到 diagnostic 模式重跑一次
（这样错误信息能显示在控制台里）。

---

## 10. 为什么界面能接受 3249 行单文件

有人会说：一个 HTML 文件 3000 多行，太不优雅了。

**我认同这是妥协，但理由充分：**

| 拆分方案的代价 | 说明 |
|---|---|
| 引入构建工具 | 需要 Node.js、npm install、构建步骤 |
| 多文件加载 | pywebview 的 http_server 要多配路由 |
| 用户成本 | `git clone` 后不能直接跑 |

**收益**：用户 clone 下来就能用，零构建。

> 💡 这是典型的**"可维护性 vs 用户成本"**权衡。
> 对一个要分发给非开发者的本地工具，**用户成本优先**。

如果将来要长期维护，可以引入一个**极轻量**的构建（比如只用原生 ES Module，
不上打包器），把代码拆成几个文件——浏览器原生支持，不需要构建。

---

## 11. 动手练习

**练习 1：验证缓存机制**

```python
import sys; sys.path.insert(0, '.')
from subgen.gui_main import versioned_url, HTML_FILE
print(versioned_url(HTML_FILE))
# 输出类似 ...\index.html?v=1792734781
```

然后**改一下 `index.html`**（随便加个空格），再打印一次。
**版本号应该变了。**

**练习 2：故意用不带版本号的 URL**

把 `gui_main.py` 里的 `versioned_url(HTML_FILE)` 改回 `HTML_FILE`，
启动界面，然后改一下 HTML 内容，重启。

**观察：改动没生效。** 这就是当初的 bug。

**练习 3：加一个新的 API**

在 `GuiApi` 里加：

```python
def hello(self, name: str = "world") -> Dict[str, Any]:
    return {"ok": True, "msg": f"你好，{name}"}
```

在界面控制台调：

```javascript
await window.pywebview.api.hello("汪兴祥")
```

**体会一下这条通道有多直接。**

**练习 4：检查设置文件**

改几个设置，然后看 `.gui_settings.json`：

```bash
cat .gui_settings.json
```

确认改动被持久化了。然后清空这个文件，重启界面，
**应该回到默认设置。**

---

## 12. 小结

**三句话**：

1. **pywebview 用系统 WebView2**，安装包 1 MB（vs Electron 150 MB），
   代价是有些能力要下沉到原生层（如拖放）
2. **两条通信通道**：JS 调 Python 走 `js_api`，Python 推 JS 走**事件队列 + 轮询**
3. **WebView2 会强缓存页面**——必须给 URL 加版本号，
   否则改了界面用户看到的还是旧的

**下一篇** → [13 · 踩坑合集](13-pitfalls.md) ⭐⭐
**这是整套教程最有价值的一篇**：12 个真实 bug，每个都有完整的
「现象 → 误判 → 真相 → 修复」过程。这些是别处学不到的。
