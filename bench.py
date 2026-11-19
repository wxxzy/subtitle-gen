import os, time, json
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
from faster_whisper import WhisperModel
from subgen.engine import _ensure_cuda_dlls
_ensure_cuda_dlls()

MODEL = "models/deepdml--faster-whisper-large-v3-turbo-ct2"
files = [("bench/tts_zh.wav", 24.0), ("bench/long_9min.wav", 547.8)]
rows = []

configs = [
    ("cuda", "float32", 5),
    ("cuda", "float32", 1),
    ("cuda", "float16", 5),
    ("cpu",  "int8",    5),
]

for dev, ct, beam in configs:
    try:
        t0 = time.time()
        m = WhisperModel(MODEL, device=dev, compute_type=ct, cpu_threads=8)
        load = time.time() - t0
    except Exception as e:
        print(f"SKIP {dev}/{ct}: {type(e).__name__}: {e}", flush=True)
        continue

    for path, dur in files:
        if not os.path.isfile(path):
            continue
        if dev == "cpu" and dur > 100:
            continue  # CPU 跑长文件太慢，跳过
        try:
            t0 = time.time()
            segs, info = m.transcribe(path, language="zh", beam_size=beam,
                                      vad_filter=True, word_timestamps=True)
            n = sum(1 for _ in segs)
            el = time.time() - t0
            row = dict(device=dev, compute=ct, beam=beam, file=os.path.basename(path),
                       audio_s=round(dur,1), infer_s=round(el,2),
                       realtime_x=round(dur/el,1), segments=n, load_s=round(load,1))
            rows.append(row)
            print(f"{dev:5}/{ct:8} beam={beam}  {row['file']:16} {dur:6.1f}s -> {el:6.2f}s  {row['realtime_x']:5.1f}x  segs={n}  load={load:.1f}s", flush=True)
        except Exception as e:
            print(f"FAIL {dev}/{ct} {path}: {e}", flush=True)
    del m
    try:
        import ctranslate2, gc; gc.collect()
    except Exception: pass

with open("bench_result.json","w",encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print("\nsaved bench_result.json")
