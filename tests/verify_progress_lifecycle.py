"""验证心跳在任务结束后停止，且不再重复推送进度。

━━━ 这个测试守的是真实事故 ━━━

症状：任务明明跑完了，界面状态栏一直显示"处理中"，
日志被"导出字幕文件…"刷到 700 多条。

根因：CallbackReporter 的心跳线程靠 file_done()/finish() 停止，
但早期 gui.py 从头到尾**没调用过这两个方法**，
于是任务结束后心跳仍在每秒推送 onProgress（stage 停在 export），
把 onJobDone 刚写上的"全部完成"覆盖掉。

两个看起来毫不相关的现象，其实是同一个根因。
"""

from __future__ import annotations

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subgen.reporter import CallbackReporter  # noqa: E402
from subgen.tasks import RunOptions, run_one  # noqa: E402

D = r"C:\workspaces\xhs-video-downloader\downloads\67a4612d000000000d009085"


def main() -> int:
    fails = []

    def chk(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
        if not cond:
            fails.append(name)

    print("=" * 62)
    print("心跳停止与日志量 · 验证")
    print("=" * 62)

    vids = sorted(glob.glob(os.path.join(D, "*.mp4")))
    if not vids:
        print("找不到测试视频")
        return 1
    V = vids[-1]

    # ---------- ① Reporter 层面：finish() 必须停心跳 ----------
    print("\n[1] Reporter 的心跳控制")
    events = []
    rep = CallbackReporter(on_progress=lambda p: events.append((time.time(), p.stage)))
    rep.stage("export", "导出字幕文件…")
    rep.update(5.0)
    time.sleep(1.4)
    during = len(events)
    chk("心跳运行期间会持续推送", during >= 1, f"收到 {during} 条")

    rep.file_done(1, 1, "x.mp4", True)
    time.sleep(1.2)
    after_done = len(events)
    time.sleep(1.5)
    chk("file_done 之后心跳停止", len(events) == after_done,
        f"file_done 后仍在推送 {len(events) - after_done} 条")

    chk("file_done 后状态是 done", rep._p.stage == "done", rep._p.stage)

    # finish() 要幂等
    rep2 = CallbackReporter(on_progress=lambda p: None)
    rep2.stage("asr", "识别中…")
    rep2.finish()
    rep2.finish()          # 重复调用不应抛异常
    chk("finish 可重复调用", rep2._p.stage == "done", rep2._p.stage)

    # ---------- ② 已用时间的基准必须是"任务开始"而不是"程序启动"
    print("\n[2] 已用时间的计时基准")
    # 早期用模块级 _T0（import 时刻），导致程序开着 47 分钟再点开始，
    # 第一次任务就显示"已用 47:39"（真实事故）。
    rep_t = CallbackReporter(on_progress=lambda p: None)
    rep_t.stage("asr", "识别中…")
    chk("新建 reporter 的 elapsed 接近 0", rep_t._p.elapsed < 1.0,
        f"elapsed={rep_t._p.elapsed:.1f}s（若很大说明用了模块加载时刻）")
    # 模拟"程序已运行很久"：把实例的 t0 往前拨，elapsed 应随实例走
    rep_t._t0 -= 600
    rep_t.update(1.0)
    chk("elapsed 跟随实例的开始时刻", rep_t._p.elapsed >= 600,
        f"elapsed={rep_t._p.elapsed:.1f}")
    rep_t2 = CallbackReporter(on_progress=lambda p: None)
    chk("新实例不受上一个影响", rep_t2._p.elapsed < 1.0,
        f"{rep_t2._p.elapsed:.1f}")

    # ---------- ③ 真实任务：结束后不应再有事件 ----------
    print("\n[3] 真实任务结束后的静默期")
    ev2 = []
    rep3 = CallbackReporter(on_progress=lambda p: ev2.append(p.stage))
    opts = RunOptions(model="turbo", language="zh", formats=["srt"],
                      output=os.path.abspath("out/hbtest"),
                      max_chars=21, max_duration=7.0)
    rep3.file_start(1, 1, os.path.basename(V))
    t0 = time.time()
    res = run_one(V, opts, reporter=rep3)
    n_end = len(ev2)
    print(f"       处理用时 {time.time() - t0:.1f}s，期间 {n_end} 条事件")

    # 模拟 gui.py 的收尾（这正是之前漏掉的一步）
    rep3.finish()
    time.sleep(3.5)
    chk("任务结束后无新事件", len(ev2) == n_end + 1,   # +1 是 finish 自己发的那条
        f"结束后又来了 {len(ev2) - n_end - 1} 条")

    stages_after = ev2[n_end:]
    chk("收尾事件是 done 状态",
        not stages_after or all(s in ("done",) for s in stages_after),
        str(stages_after))

    print(f"\n       最终事件序列尾部: {ev2[-4:]}")

    import shutil
    shutil.rmtree(os.path.abspath("out/hbtest"), ignore_errors=True)
    shutil.rmtree(os.path.abspath("out/hb"), ignore_errors=True)

    print("\n" + "=" * 62)
    if fails:
        print(f"失败 {len(fails)} 项：{', '.join(fails)}")
        print("\n提示：若「结束后无新事件」失败，说明心跳没停，")
        print("      检查 tasks/gui 是否调用了 rep.file_done() 与 rep.finish()。")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
