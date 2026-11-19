"""媒体服务：把本地视频/音频以 HTTP 方式提供给界面播放。

为什么要自己写而不用 python 内置的 SimpleHTTPRequestHandler：
内置实现**不支持 Range 请求**（实测返回 200 全量传输，没有 206）。
而"对着视频逐句修正字幕"的核心操作就是反复跳转定位，
没有 Range 就没法拖动进度条，每次跳转都要重新下载整个文件——完全不可用。

这里实现了标准的 HTTP Range（含单段请求与边界处理），
浏览器内核（WebView2）就能像播放网络视频一样任意 seek。
"""

from __future__ import annotations

import mimetypes
import os
import posixpath
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

# 只允许访问注册过的文件，避免把整个磁盘暴露出去
_ALLOWED: Dict[str, str] = {}
_ALLOW_LOCK = threading.Lock()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def register(path: str) -> str:
    """把一个本地文件注册为可访问资源，返回其 URL 路径（形如 /media/xxx.mp4）。"""
    ap = os.path.abspath(path)
    key = posixpath.basename(ap.replace("\\", "/"))
    # 同名文件加后缀区分，避免互相覆盖
    with _ALLOW_LOCK:
        base, ext = os.path.splitext(key)
        i = 1
        while key in _ALLOWED and _ALLOWED[key] != ap:
            key = f"{base}_{i}{ext}"
            i += 1
        _ALLOWED[key] = ap
    return "/media/" + urllib.parse.quote(key)


def unregister(url_path: str) -> None:
    key = urllib.parse.unquote(url_path.split("/media/", 1)[-1])
    with _ALLOW_LOCK:
        _ALLOWED.pop(key, None)


def _lookup(url_path: str) -> Optional[str]:
    if not url_path.startswith("/media/"):
        return None
    key = urllib.parse.unquote(url_path[len("/media/"):])
    with _ALLOW_LOCK:
        return _ALLOWED.get(key)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 必须 1.1，否则 keep-alive 与 Range 都会退化

    # 静音日志，避免刷屏
    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def _send_file(self, path: str) -> None:
        try:
            st = os.stat(path)
        except OSError:
            self.send_error(404, "File not found")
            return

        size = st.st_size
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if rng:
            m = _RANGE_RE.match(rng.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1 == "" and g2:
                    # bytes=-N 表示最后 N 字节
                    n = int(g2)
                    start = max(0, size - n)
                    end = size - 1
                else:
                    start = int(g1) if g1 else 0
                    end = int(g2) if g2 else size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True

        length = end - start + 1

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")     # 告诉播放器支持分段
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # 本地应用，放开缓存无妨
        self.send_header("Cache-Control", "no-store")
        # ★ CORS 必须放开，否则界面里的 Web Audio API 拿不到音频数据 ——
        # 界面是 pywebview 的 http_server 提供的，端口与本服务不同，
        # 属于跨源；跨源媒体接进 AudioContext 会被静默处理成静音（不报错，很难查）。
        # 只允许本机，且服务本身只绑 127.0.0.1，不构成风险。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Range, Content-Length, Accept-Ranges")
        self.end_headers()

        if self.command == "HEAD":
            return

        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 播放器 seek 时会主动断开旧连接，属正常现象，不必报错
            pass

    def do_GET(self):
        path = _lookup(self.path.split("?", 1)[0])
        if not path or not os.path.isfile(path):
            self.send_error(404, "Not registered")
            return
        self._send_file(path)

    def do_HEAD(self):
        self.do_GET()


class MediaServer:
    """本地媒体服务。绑定 127.0.0.1，仅本机可访问。"""

    def __init__(self) -> None:
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    def start(self) -> int:
        if self._httpd is not None:
            return self.port
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="media-server"
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def url_for(self, path: str) -> str:
        """返回可直接给 <video src> 用的完整 URL。"""
        if self._httpd is None:
            self.start()
        return f"http://127.0.0.1:{self.port}{register(path)}"


_SERVER = MediaServer()


def get_server() -> MediaServer:
    return _SERVER
