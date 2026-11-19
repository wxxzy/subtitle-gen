"""进度上报与取消控制。

为什么要抽这一层：CLI 想往终端打进度条，GUI 想把进度推给网页，
两者的输出通道完全不同。早期版本直接在 process_one 里 print()，
导致 GUI 没法复用同一条处理链路。

现在核心只依赖 Reporter 接口，CLI 和 GUI 各自实现一份：

    CliReporter  -> 写 stdout（保持原有终端观感）
    GuiReporter  -> 回调 webview 的 JS，并检查取消标志

这样 GUI 与 CLI 走的是**同一套处理逻辑**，不会出现"GUI 能跑 CLI 报错"
这类双份维护的坑。
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class Cancelled(Exception):
    """用户主动取消。"""


@dataclass
class Progress:
    """一条进度事件。GUI 与 CLI 共用同一份数据结构。"""

    stage: str = ""              # 阶段标识：audio / load / asr / translate / export / done
    stage_label: str = ""        # 阶段中文名，直接展示
    current: float = 0.0         # 当前进度值（秒 或 条数）
    total: float = 0.0           # 总量
    file_index: int = 0          # 第几个文件（从 1 起）
    file_count: int = 0
    file_name: str = ""
    extra: str = ""              # 附加说明（如"3.5x 实时"）
    elapsed: float = 0.0
    # 心跳：阶段内已持续多久。有些步骤（加载模型、翻译大模型）长时间没有
    # 细粒度进度，界面靠这个显示"仍在处理，已用时 X 秒"，避免用户以为卡死。
    stage_elapsed: float = 0.0
    # 实时倍率（音频秒数 / 处理秒数）。用于预估剩余时间。
    speed: float = 0.0

    @property
    def ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.current / self.total))


class Reporter:
    """进度上报接口。核心代码只调这几个方法。"""

    def log(self, msg: str) -> None:
        ...

    def stage(self, stage: str, label: str, *, total: float = 0.0) -> None:
        ...

    def update(self, current: float, *, extra: str = "") -> None:
        ...

    def file_start(self, index: int, count: int, name: str) -> None:
        ...

    def file_done(self, index: int, count: int, name: str, ok: bool, msg: str = "") -> None:
        """一个文件处理完毕。**必须调用**，否则心跳不会停。"""
        ...

    def finish(self) -> None:
        """整批任务结束。停掉心跳等后台活动。

        ★ 为什么必须有这个方法（真实事故）★
        早期只定义了 file_done() 却从没人调用，导致心跳线程**永不停止**：
        任务跑完了还在每秒推送 onProgress（stage 停留在 export），
        后果是
          - 日志被"导出字幕文件…"刷屏（实测刷到 732 条）
          - 状态栏刚写上的"全部完成"立刻被心跳覆盖，永远显示"处理中"
        两个现象看起来毫不相干，其实是同一个根因。
        所以这里显式提供 finish()，并由调用方在 finally 里兜底调用。
        """
        ...

    def check_cancel(self) -> None:
        """在耗时循环里定期调用；用户取消则抛 Cancelled。"""

    @property
    def cancelled(self) -> bool:
        return False


class NullReporter(Reporter):
    """什么都不做。用于库调用或测试。"""


class CliReporter(Reporter):
    """终端输出。保持与原有 CLI 观感一致。"""

    def __init__(self, *, quiet: bool = False, width: int = 30) -> None:
        self.quiet = quiet
        self.width = width
        self._t0 = time.time()
        self._last_draw = 0.0
        self._cur_total = 0.0

    # -- 基础
    def log(self, msg: str) -> None:
        if not self.quiet:
            # 进度条可能已占一行，先换行避免覆盖
            if self._last_draw:
                print()
                self._last_draw = 0.0
            print(msg, flush=True)

    def stage(self, stage: str, label: str, *, total: float = 0.0) -> None:
        self._cur_total = total
        self._last_draw = 0.0
        if not self.quiet:
            print(label, flush=True)

    def update(self, current: float, *, extra: str = "") -> None:
        if self.quiet or self._cur_total <= 0:
            return
        now = time.time()
        # 限制刷新频率，避免刷屏拖慢写日志
        if now - self._last_draw < 0.08 and current < self._cur_total:
            return
        self._last_draw = now
        pct = max(0.0, min(1.0, current / self._cur_total))
        n = int(pct * self.width)
        bar = "#" * n + "." * (self.width - n)
        tail = f"  {extra}" if extra else ""
        print(f"      [{bar}] {pct * 100:5.1f}%  {current:.1f}/{self._cur_total:.1f}s{tail}",
              end="\r", flush=True)

    def file_start(self, index: int, count: int, name: str) -> None:
        if not self.quiet:
            print(f"\n### [{index}/{count}] {name}" if count > 1 else "", flush=True)

    def file_done(self, index: int, count: int, name: str, ok: bool, msg: str = "") -> None:
        if self.quiet:
            return
        if self._last_draw:
            print(" " * 80, end="\r")
            self._last_draw = 0.0
        if not ok:
            print(f"      x {name}: {msg}", flush=True)

    def finish(self) -> None:
        """收尾：把进度条那一行清干净，避免残留半行字符。"""
        if self.quiet:
            return
        if self._last_draw:
            print(" " * 80, end="\r")
            self._last_draw = 0.0


class CallbackReporter(Reporter):
    """把进度事件转成回调。GUI 用它把事件推给网页。

    回调可能在非主线程被调用（处理跑在后台线程），
    所以调用方需自行保证线程安全（GUI 侧用 webview.evaluate_js 是安全的）。
    """

    def __init__(
        self,
        on_progress: Optional[Callable[[Progress], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        heartbeat: float = 1.0,
    ) -> None:
        self._on_progress = on_progress
        self._on_log = on_log
        self._cancel = cancel_event or threading.Event()
        self._p = Progress()
        self._last_emit = 0.0
        # ★ 每个 reporter 实例自己记开始时刻。
        #
        # 早期用的是模块级的 _T0 = time.time()，那是**模块被 import 的时刻**，
        # 跟任务何时开始毫无关系。后果：程序开着 47 分钟才点"开始生成"，
        # 第一次任务就会显示"已用 47:39"，用户以为卡了 47 分钟（真实踩过）。
        self._t0 = time.time()
        self._stage_t0 = 0.0
        # 心跳：有些步骤长时间没有细粒度进度（加载模型、LLM 翻译），
        # 靠定时重发当前状态让界面显示"仍在处理，已用时 X 秒"，
        # 用户就不会以为卡死了。
        self._hb_interval = heartbeat
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()
        self._hb_ended = False

    # ---- 心跳
    def start_heartbeat(self) -> None:
        if self._hb_thread or self._hb_interval <= 0:
            return
        self._hb_stop.clear()
        self._hb_ended = False

        def loop():
            while not self._hb_stop.wait(self._hb_interval):
                if self._cancel.is_set():
                    continue
                # 阶段已经结束就不该再发心跳。
                # 这是第二道防线：即便调用方忘了 file_done()/finish()，
                # 也不会一直刷"处理中"（真实事故的根因就是漏调 file_done）。
                if self._hb_ended or self._p.stage in ("done", "error"):
                    break
                if self._stage_t0:
                    self._p.stage_elapsed = time.time() - self._stage_t0
                self._emit(force=True)

        self._hb_thread = threading.Thread(target=loop, daemon=True, name="rep-heartbeat")
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._hb_ended = True
        self._hb_stop.set()
        self._hb_thread = None

    # -- 取消
    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled("用户取消")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    # -- 上报
    def log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    def _emit(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < 0.12:
            return
        self._last_emit = now
        if self._on_progress:
            self._on_progress(self._p)

    def stage(self, stage: str, label: str, *, total: float = 0.0) -> None:
        self._p.stage = stage
        self._p.stage_label = label
        self._p.total = total
        self._p.current = 0.0
        self._p.stage_elapsed = 0.0
        self._p.speed = 0.0
        self._stage_t0 = time.time()
        self._p.elapsed = self._stage_t0 - self._t0
        self._emit(force=True)
        self.start_heartbeat()

    def update(self, current: float, *, extra: str = "") -> None:
        self._p.current = current
        if extra:
            self._p.extra = extra
        now = time.time()
        self._p.elapsed = now - self._t0
        if self._stage_t0:
            self._p.stage_elapsed = now - self._stage_t0
            # 实时倍率：已处理音频秒数 / 已耗时。用于界面预估剩余时间。
            spent = self._p.stage_elapsed
            if spent > 1.0 and current > 0:
                self._p.speed = current / spent
        self._emit()

    def file_start(self, index: int, count: int, name: str) -> None:
        self._p.file_index = index
        self._p.file_count = count
        self._p.file_name = name
        self._emit(force=True)

    def file_done(self, index: int, count: int, name: str, ok: bool, msg: str = "") -> None:
        self.stop_heartbeat()          # ★ 关键：不停心跳会导致刷屏与状态栏卡死
        self._p.file_index = index
        self._p.file_count = count
        self._p.file_name = name
        self._p.stage = "done" if ok else "error"
        self._p.stage_label = "完成" if ok else f"失败：{msg}"
        self._emit(force=True)

    def finish(self) -> None:
        """整批任务收尾：停心跳，并标记为结束状态。

        幂等，可以重复调用（在 finally 里兜底很安全）。
        """
        self.stop_heartbeat()
        self._cancel.set()
        self._p.stage = "done"
        self._p.stage_label = "完成"
        self._p.stage_elapsed = 0.0
        self._emit(force=True)

