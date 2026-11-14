"""Download core, independent of the desktop UI."""

import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import deno
import imageio_ffmpeg
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import download_range_func

from clip_downloader.history import download_lock, find_existing


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    directory: str
    quality: str = "1080p"
    start: float | None = None
    end: float | None = None
    skip_existing: bool = True

    def validate(self):
        parsed = urlsplit(self.url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise ValueError("http / https の動画URLを入力してください。")
        if parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com") and (
            parsed.path in ("/playlist", "/feed/subscriptions")
        ):
            raise ValueError("プレイリストではなく、単体動画のURLを指定してください。")
        if parsed.username or parsed.password:
            raise ValueError("認証情報を含むURLは使用できません。")
        if not self.directory.strip() or not Path(self.directory).is_absolute():
            raise ValueError("保存先は絶対パスで指定してください。")
        if self.quality not in ("720p", "1080p", "最高画質"):
            raise ValueError("画質を選択してください。")
        if (self.start is None) != (self.end is None):
            raise ValueError("区間の開始と終了を両方指定してください。")
        if self.start is not None and (
            not math.isfinite(self.start) or not math.isfinite(self.end)
            or self.start < 0 or self.end <= self.start
        ):
            raise ValueError("終了時刻は開始時刻より後にしてください。")


def parse_time(value: str) -> float:
    value = value.strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", value):
        raise ValueError("時刻は秒、分:秒、時:分:秒で入力してください。")
    parts = [float(part) for part in value.split(":")]
    if len(parts) > 1 and any(part >= 60 for part in parts[1:]):
        raise ValueError("時刻の分・秒は60未満にしてください。")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    if not math.isfinite(seconds):
        raise ValueError("時刻が大きすぎます。")
    return seconds


def deno_path() -> str:
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "deno.exe"
        if bundled.is_file():
            return str(bundled)
    return deno.find_deno_bin()


def build_options(request: DownloadRequest, folder: Path, progress, logger) -> dict:
    height = "" if request.quality == "最高画質" else f"[height<=?{request.quality[:-1]}]"
    options = {
        "format": f"bv*{height}+ba/b{height}",
        "outtmpl": str(folder / "%(title).100s [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "js_runtimes": {"deno": {"path": deno_path()}},
        "noplaylist": True,
        "playlistend": 1,
        "windowsfilenames": True,
        "overwrites": False,
        "quiet": True,
        "noprogress": True,
        "logger": logger,
        "progress_hooks": [progress],
        "postprocessor_hooks": [lambda _: progress({"status": "processing"})],
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
    }
    if request.start is not None:
        options["download_ranges"] = download_range_func(None, [(request.start, request.end)])
    return options


def run_download(request: DownloadRequest, emit) -> dict:
    request.validate()
    root = Path(request.directory)
    # Do not silently create a substitute for a missing Windows drive.
    if os.name == "nt" and not Path(root.anchor).exists():
        raise ValueError("保存先のドライブが接続されていません。")
    root.mkdir(parents=True, exist_ok=True)
    with download_lock(request, emit):
        if request.skip_existing:
            emit({"type": "download_phase", "text": "保存済み動画の履歴を確認中…"})
            started = time.monotonic()
            existing = find_existing(request)
            emit({"type": "log", "text": f"履歴確認: {time.monotonic() - started:.2f}秒"})
            if existing:
                emit({"type": "folder", "path": str(Path(existing["output"]).parent)})
                return existing
        return _download_new(request, root, emit)


def _download_new(request, root, emit):
    folder = root / (time.strftime("clip-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    folder.mkdir()
    emit({"type": "folder", "path": str(folder)})
    last = 0.0

    def progress(data):
        nonlocal last
        now = time.monotonic()
        if data.get("status") == "downloading" and now - last < 0.25:
            return
        last = now
        emit({"type": "progress", "status": data.get("status"),
              "downloaded": data.get("downloaded_bytes", 0),
              "total": data.get("total_bytes") or data.get("total_bytes_estimate"),
              "speed": data.get("speed"), "eta": data.get("eta")})

    class Logger:
        def debug(self, message):
            # yt-dlp sends ordinary extractor messages through debug too.
            if not str(message).startswith("[debug]"):
                emit({"type": "log", "text": str(message)})
            if str(message).startswith("[info]") and "Downloading" in str(message):
                emit({"type": "download_phase", "text": "動画・音声の取得開始待ち…"})

        def warning(self, message):
            emit({"type": "log", "text": str(message)})

        def error(self, message):
            emit({"type": "log", "text": str(message)})

    options = build_options(request, folder, progress, Logger())
    # yt-dlp's CLI also sets this context: FFmpegFD.available() has no downloader instance.
    token = FFmpegPostProcessor._ffmpeg_location.set(options["ffmpeg_location"])
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            emit({"type": "download_phase", "text": "YouTube等の配信元から情報取得中…"})
            info = downloader.extract_info(request.url, download=True)
            if not info or info.get("_type") in ("playlist", "multi_video"):
                raise ValueError("単体の動画URLを指定してください。")
    finally:
        FFmpegPostProcessor._ffmpeg_location.reset(token)
    outputs = [p for p in folder.glob("*.mp4") if p.stat().st_size > 0]
    if len(outputs) != 1:
        raise RuntimeError("完成したMP4を確認できません。保存先とログを確認してください。")
    result = {"version": 1, "request": asdict(request), "title": info.get("title"),
              "source_url": info.get("webpage_url") or request.url,
              "source_id": info.get("id"), "output": str(outputs[0]),
              "output_size": outputs[0].stat().st_size,
              "requested_range": [request.start, request.end],
              "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (folder / "clip.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return result


def worker(request, queue):
    try:
        from clip_downloader.local_clip import LocalClipRequest, run_local_clip
        result = (run_local_clip(request, queue.put) if isinstance(request, LocalClipRequest)
                  else run_download(request, queue.put))
        queue.put({"type": "done", "result": result})
    except Exception as error:  # noqa: BLE001 - serialize worker failure to the UI
        queue.put({"type": "error", "text": str(error)})
