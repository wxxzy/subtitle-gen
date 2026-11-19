"""模型预设、设备探测与下载。

针对不同硬件给不同默认值。特别地，GTX 1080 Ti 属于 Pascal（sm_61）架构，
没有 Tensor Core，且 FP16 吞吐被严重阉割——这类卡上用 float16 反而可能比 float32 慢，
所以这里按 compute capability 自动选择计算精度。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from typing import Optional, Tuple

# 国内可直连的镜像，加速首次下载
HF_ENDPOINT = os.environ.get("SUBGEN_HF_ENDPOINT", "https://hf-mirror.com")


@dataclass(frozen=True)
class ModelPreset:
    key: str
    repo: str
    label: str
    vram_gb: float
    note: str
    alias: tuple = ()


# 全部为 CTranslate2 转换版（faster-whisper 直接可用）
PRESETS: dict[str, ModelPreset] = {
    "turbo": ModelPreset(
        "turbo", "deepdml/faster-whisper-large-v3-turbo-ct2",
        "large-v3-turbo（推荐）", 2.5,
        "4 层解码器，速度约为 large-v3 的 3~5 倍，精度几乎持平；中英混排首选",
        ("large-v3-turbo", "large_turbo"),
    ),
    "large": ModelPreset(
        "large", "Systran/faster-whisper-large-v3",
        "large-v3（最准）", 4.7,
        "精度天花板，慢一些；对难缠音频、专业术语场景更适合",
        ("large-v3", "large-v3"),
    ),
    "medium": ModelPreset(
        "medium", "Systran/faster-whisper-medium",
        "medium（均衡）", 2.5,
        "老显卡或显存紧张时的稳妥选择",
    ),
    "small": ModelPreset(
        "small", "Systran/faster-whisper-small",
        "small（轻量）", 1.2,
        "480 MB，最快；适合先看效果或纯 CPU 场景",
    ),
    "base": ModelPreset(
        "base", "Systran/faster-whisper-base",
        "base（极速）", 0.6, "极小体积，精度有限，适合草稿",
    ),
}

ALIASES: dict[str, str] = {}
for _k, _p in PRESETS.items():
    ALIASES[_k] = _k
    ALIASES[_p.repo.lower()] = _k
    for _a in _p.alias:
        ALIASES[_a.lower()] = _k


def resolve_model(spec: str) -> str:
    """把用户输入的别名解析成模型仓库名；无法识别时原样返回（当作本地路径/HF repo）。"""
    if not spec:
        return PRESETS["turbo"].repo
    return PRESETS[ALIASES[spec.lower()]].repo if spec.lower() in ALIASES else spec


# --------------------------------------------------------------- 设备探测

def gpu_summary() -> dict:
    """返回 GPU 概况：数量、名称、compute capability、建议精度。"""
    out = {"count": 0, "names": [], "cc": None, "recommended_compute_type": "int8"}
    try:
        import ctranslate2

        out["count"] = ctranslate2.get_cuda_device_count()
    except Exception:
        out["count"] = 0

    # 用 nvidia-smi 拿型号
    try:
        import subprocess

        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if parts and parts[0]:
                    out["names"].append(parts[0])
    except Exception:
        pass

    if out["count"] == 0:
        out["recommended_compute_type"] = "int8"
        return out

    # 计算精度选择（本机实测数据，GTX 1080 Ti / Pascal CC 6.1 / 34 秒音频）：
    #   turbo float32  32.4s (1.0x)   |  turbo int8   2.5s (13.6x)
    #   large float32 125.4s (0.3x)   |  large int8   7.0s ( 4.8x)
    # 两条结论：
    #   1. int8 比 float32 快 13~18 倍，而识别结果 tokens 数完全一致，
    #      质量损失极小 —— 所以**一律优先 int8**，这与显卡新旧无关。
    #   2. float16 在 Pascal（CC<7）消费卡上被限速到 FP32 的 1/64，
    #      指定它会直接报错，必须避开。
    cc = _detect_cc(out["names"])
    out["cc"] = cc
    if cc is not None and cc < 7.0:
        out["recommended_compute_type"] = "int8"
        out["note"] = "Pascal 架构：FP16 受限，int8 量化最快"
    else:
        # 新卡上 int8 依然比 float16 快，且显存占用更低
        out["recommended_compute_type"] = "int8"
        out["note"] = "int8 量化，速度与显存均优于 float16"
    return out


def _detect_cc(names: list[str]) -> Optional[float]:
    """按型号推断 compute capability。"""
    table = {
        "GTX 1080": 6.1, "GTX 1070": 6.1, "GTX 1060": 6.1, "P40": 6.1, "P100": 6.0,
        "P4": 6.1, "GTX 1050": 6.1, "TITAN X": 6.1,
        "RTX 2060": 7.5, "RTX 2070": 7.5, "RTX 2080": 7.5, "T4": 7.5,
        "RTX 3050": 8.6, "RTX 3060": 8.6, "RTX 3070": 8.6, "RTX 3080": 8.6,
        "RTX 3090": 8.6, "A10": 8.6, "A100": 8.0, "A40": 8.6,
        "RTX 4060": 8.9, "RTX 4070": 8.9, "RTX 4080": 8.9, "RTX 4090": 8.9,
        "L4": 8.9, "L40": 8.9, "RTX 5090": 12.0, "RTX 5080": 12.0,
    }
    for n in names:
        for k, v in table.items():
            if k.lower() in n.lower():
                return v
    return None


# --------------------------------------------------------------- 下载

def model_dir_name(repo: str) -> str:
    """仓库名 -> 本地目录名。"""
    return repo.replace("/", "--")


def ensure_model(
    repo: str,
    *,
    cache_dir: str,
    mirror: bool = True,
    verbose: bool = True,
    on_progress=None,
    should_cancel=None,
) -> str:
    """确保模型在本地，返回**可直接加载的目录**（内含真实 model.bin）。

    为什么不用 `snapshot_download`：它在 local_dir 模式下会先建 blob 再建符号链接，
    而 Windows 非管理员/未开开发者模式时符号链接会失败，结果得到一堆 0 字节文件，
    模型无法加载（这个坑很隐蔽）。这里改为自己按文件清单下载并**校验大小**，
    必要时还能从已有的 blob 里按大小认领，避免重复下载。

    on_progress: 可选回调 (downloaded_bytes, total_bytes, filename, file_index, file_count)，
                 用于把下载进度显示到界面上（大模型下载要十几分钟，必须让用户看见）。
    should_cancel: 可选回调，返回 True 时中止下载。"""
    os.makedirs(cache_dir, exist_ok=True)

    # 已是含 model.bin 的本地目录
    if os.path.isdir(repo):
        if _valid_model_dir(repo):
            return os.path.abspath(repo)
        found = find_model_dir(repo)
        if found:
            return found

    if mirror:
        os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)

    endpoint = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    target = os.path.join(cache_dir, model_dir_name(repo))

    files = _list_repo_files(repo, endpoint)
    if not files:
        raise RuntimeError(f"无法获取模型文件清单：{repo}（网络不通或仓库名有误）")

    if _dir_matches(target, files):
        if verbose:
            print(f"[模型] 命中本地缓存：{target}")
        return os.path.abspath(target)

    os.makedirs(target, exist_ok=True)
    blobs = _blob_pool(cache_dir, repo)

    if verbose:
        total = sum(s for _, s in files) / 1024**3
        print(f"[模型] 首次下载 {repo}")
        print(f"[模型] 源：{endpoint}")
        print(f"[模型] 目录：{target}")
        print(f"[模型] 共 {len(files)} 个文件，约 {total:.2f} GB，请稍候……")

    # 下载前先算总量，用于展示"第几个文件 / 总进度"
    grand_total = sum(s for _, s in files) or 1
    # done_bytes 随每个文件完成而累加。
    # 别写成固定常量 —— 模型目录里除了大文件还有 config/tokenizer 等小文件，
    # 若基数不累加，下到小文件时百分比会突然跳回 0%，看着像重头开始了。
    done_bytes = 0

    for idx, (name, size) in enumerate(files, 1):
        dst = os.path.join(target, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        if os.path.isfile(dst) and os.path.getsize(dst) == size:
            done_bytes += size
            continue

        # 先从已有 blob 里按大小认领，省一次下载
        src = blobs.get(size)
        if src:
            shutil.copyfile(src, dst)
            if os.path.getsize(dst) == size:
                if verbose:
                    print(f"[模型]   {name} 从本地缓存复用")
                done_bytes += size
                continue

        if verbose:
            print(f"[模型]   下载 {name}（{size / 1024**2:.1f} MB）……", end="", flush=True)

        # 进度回调：把"当前文件已下载量"换算成"整体已下载量"，界面就能
        # 显示统一的总进度（多个文件时不会来回跳）
        base = done_bytes
        last_cb = [0.0]

        def _cb(got: int, tot: int, _name=name, _base=base, _size=size, _idx=idx):
            if not on_progress:
                return
            now = time.time()
            # 限流：0.4 秒一次，避免回调太频繁
            if now - last_cb[0] < 0.4 and (tot <= 0 or got < tot):
                return
            last_cb[0] = now
            overall = _base + got
            on_progress(overall, grand_total, _name, _idx, len(files))

        from urllib.parse import quote as _quote
        url = f"{endpoint}/{repo}/resolve/main/{_quote(name)}"
        _download_file(url, dst, on_progress=_cb,
                       resume=True,
                       attempts=(20 if should_cancel else 3))
        # 断点续传期间若用户取消，直接抛出
        if should_cancel and should_cancel():
            raise RuntimeError("已取消")
        got = os.path.getsize(dst) if os.path.isfile(dst) else 0
        if got != size:
            raise RuntimeError(f"文件大小校验失败：{name} 期望 {size} 实际 {got}")
        done_bytes += size
        if on_progress:
            on_progress(done_bytes, grand_total, name, idx, len(files))
        if verbose:
            print(" 完成")

    if not _valid_model_dir(target):
        raise RuntimeError(f"模型校验失败，未找到有效的 model.bin：{target}")
    if verbose:
        print(f"[模型] 就绪：{target}")
    return os.path.abspath(target)


def _list_repo_files(repo: str, endpoint: str) -> list[tuple[str, int]]:
    """通过 HF API 取文件清单（含大小）。"""
    import json
    import urllib.request

    url = f"{endpoint}/api/models/{repo}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "subgen/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []

    out: list[tuple[str, int]] = []
    for s in data.get("siblings", []):
        name = s.get("rfilename", "")
        size = s.get("size")
        if not name or size is None:
            continue
        if name.endswith((".md", ".gitattributes")):
            continue
        out.append((name, int(size)))
    return out


def _download_file(
    url: str,
    dst: str,
    *,
    on_progress=None,
    resume: bool = True,
    attempts: int = 3,
) -> None:
    """下载单个文件，支持断点续传与进度回调。

    两个都重要：
    - **进度回调**：模型动辄 1.5~3 GB，没有进度用户只能干等十几分钟，
      完全不知道是在下载还是卡死了（这是踩过的坑）。
    - **断点续传**：2.9 GB 下到 90% 断了，从头再来会让人崩溃。
      用 HTTP Range 从已下载的字节数继续。
    """
    import urllib.error
    import urllib.request

    last: Exception | None = None
    for attempt in range(attempts):
        have = os.path.getsize(dst) if (resume and os.path.isfile(dst)) else 0
        try:
            headers = {"User-Agent": "subgen/1.0"}
            if have > 0:
                headers["Range"] = f"bytes={have}-"

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as r:
                code = getattr(r, "status", 200)
                # 206 = 服务端接受了 Range，从 have 继续；200 = 不支持，只能重下
                if have > 0 and code == 200:
                    have = 0
                    mode = "wb"
                else:
                    mode = "ab" if have > 0 else "wb"

                clen = int(r.headers.get("Content-Length") or 0)
                total = have + clen if clen else 0

                with open(dst, mode) as f:
                    got = have
                    while True:
                        chunk = r.read(1024 * 512)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if on_progress:
                            on_progress(got, total)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            # 注意：不再删除已下载的部分，留给下次断点续传
            if not resume and os.path.isfile(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            time.sleep(1.0)
    raise RuntimeError(f"下载失败：{url}\n{last}")


def _blob_pool(cache_dir: str, repo: str) -> dict[int, str]:
    """扫描 HF 默认缓存里的 blob，按大小建索引，用于复用已下载数据。"""
    pool: dict[int, str] = {}
    candidates = [
        os.path.join(cache_dir, "models--" + model_dir_name(repo).replace("/", "--"), "blobs"),
    ]
    for c in candidates:
        if not os.path.isdir(c):
            continue
        for fn in os.listdir(c):
            p = os.path.join(c, fn)
            if os.path.isfile(p):
                sz = os.path.getsize(p)
                if sz > 1024:
                    pool.setdefault(sz, p)
    return pool


def _dir_matches(target: str, files: list[tuple[str, int]]) -> bool:
    if not _valid_model_dir(target):
        return False
    for name, size in files:
        p = os.path.join(target, name.replace("/", os.sep))
        if not os.path.isfile(p) or os.path.getsize(p) != size:
            return False
    return True


def _valid_model_dir(d: str) -> bool:
    f = os.path.join(d, "model.bin")
    return os.path.isfile(f) and os.path.getsize(f) > 1024


def find_model_dir(root: str, max_depth: int = 4) -> Optional[str]:
    """在 root 下递归找一个含有效 model.bin 的目录。"""
    root = os.path.abspath(root)
    for cur, dirs, files in os.walk(root):
        if cur[len(root):].count(os.sep) > max_depth:
            dirs[:] = []
            continue
        if "model.bin" in files and os.path.getsize(os.path.join(cur, "model.bin")) > 1024:
            return cur
    return None
