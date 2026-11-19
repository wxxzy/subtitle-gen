"""subgen —— 视频/音频自动生成字幕、并支持多语种翻译的本地引擎。

模块划分：
    models     模型预设、设备探测、下载
    audio      ffmpeg 抽音轨 / 取时长 / 烧录硬字幕
    engine     faster-whisper 推理封装
    segmenter  智能断句（把 token 流切成适合上屏的字幕行）+ 折行
    translator 翻译引擎（离线 CT2 模型 / LLM 双后端）+ 术语表保护
    formats    SRT / VTT / ASS / LRC / TXT 输出（支持双语）
    cli        命令行入口
"""

__version__ = "1.1.0"
__all__ = ["__version__"]
