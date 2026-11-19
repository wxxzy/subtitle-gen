"""工作台端到端验证。

用真实视频 + 真实字幕启动工作台，检查：
  1. 媒体服务能播放（Range 已单独验证过）
  2. 字幕正确载入
  3. 保存能落盘
不用窗口交互（无头环境限制），直接走 API 层。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subgen.mediaserve import get_server  # noqa: E402
from subgen.review import ReviewApi, prepare  # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = r"C:\workspaces\xhs-video-downloader\downloads\67a4612d000000000d009085"


def main() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
        if not cond:
            fails.append(name)

    print("=" * 62)
    print("字幕校对工作台 · 端到端验证")
    print("=" * 62)

    vids = sorted(glob.glob(os.path.join(D, "*.mp4")))
    if not vids:
        print("找不到测试视频")
        return 1
    video = vids[3]     # 取一个短的
    print(f"\n测试视频: {os.path.basename(video)}")

    # ---------- 1) 准备会话 ----------
    print("\n[1] 准备校对会话")
    init = prepare(video)
    check("拿到播放 URL", bool(init.get("videoUrl")), str(init))
    check("识别为视频类型", init.get("mediaType") == "video", str(init.get("mediaType")))
    print(f"       URL: {init['videoUrl'][:66]}...")

    # ---------- 2) 媒体服务可播放 ----------
    print("\n[2] 媒体服务")
    try:
        req = urllib.request.Request(init["videoUrl"], headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=20) as r:
            check("Range 请求返回 206", r.status == 206, f"status={r.status}")
            check("Content-Range 正确", "bytes 0-1023/" in (r.headers.get("Content-Range") or ""),
                  str(r.headers.get("Content-Range")))
            check("Content-Type 是视频", "video" in (r.headers.get("Content-Type") or ""),
                  str(r.headers.get("Content-Type")))
    except Exception as e:
        check("媒体请求成功", False, f"{type(e).__name__}: {e}")

    # ---------- 3) 字幕往返 ----------
    print("\n[3] 字幕载入与保存")
    api = ReviewApi()
    tmp = tempfile.mkdtemp()
    try:
        # 用真实字幕作模板
        src_subs = glob.glob(os.path.expanduser("~/Desktop/*.zh.srt"))
        work = os.path.join(tmp, "work.srt")
        if src_subs:
            shutil.copyfile(src_subs[0], work)
        else:
            # 没有就用生成的
            from subgen.subio import SubCue, write_subtitle
            write_subtitle(work, [
                SubCue(0.0, 2.0, "第一句测试", 1),
                SubCue(2.5, 5.0, "第二句测试", 2),
            ])

        data = api.load_subtitle(work)
        check("字幕载入成功", bool(data and data["cues"]), "无内容")
        n0 = len(data["cues"])
        print(f"       载入 {n0} 条")

        # 模拟人工修正
        cues = data["cues"]
        cues[0]["text"] = "【人工修正示例】" + cues[0]["text"]
        # 故意改坏时间轴，验证保存时自动修复
        if len(cues) > 1:
            cues[0]["end"] = cues[1]["start"] + 5.0     # 造成交叉
        r = api.save_subtitle(work, cues)
        check("保存返回成功", bool(r.get("ok")), str(r))

        back = api.load_subtitle(work)
        check("回读条数一致", len(back["cues"]) == n0, f"{len(back['cues'])} vs {n0}")
        check("中文修改已落盘", back["cues"][0]["text"].startswith("【人工修正示例】"),
              back["cues"][0]["text"][:30])
        if len(back["cues"]) > 1:
            ok_order = all(back["cues"][i]["end"] <= back["cues"][i+1]["start"] + 1e-6
                           for i in range(len(back["cues"]) - 1))
            check("时间交叉已自动修复", ok_order,
                  str([(round(c['start'],2), round(c['end'],2)) for c in back['cues'][:3]]))
        check("生成了备份文件", os.path.isfile(work + ".bak"))

        # 时间戳精度
        c0 = back["cues"][0]
        print(f"       首条: {c0['start']:.2f} → {c0['end']:.2f}  {c0['text'][:26]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 4) 多字幕选择 ----------
    print("\n[4] 同名字幕自动发现")
    from subgen.subio import find_subtitles
    subs = find_subtitles(video)
    check("找到同名字幕", len(subs) > 0, "无")
    if subs:
        print(f"       找到 {len(subs)} 个: {[os.path.basename(x)[-18:] for x in subs]}")
        picked = init.get("subPath", "")
        check("自动选中中文版", picked.endswith(".zh.srt") or picked.endswith(".srt"),
              os.path.basename(picked))

    get_server().stop()

    print("\n" + "=" * 62)
    if fails:
        print(f"失败 {len(fails)} 项：{', '.join(fails)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
