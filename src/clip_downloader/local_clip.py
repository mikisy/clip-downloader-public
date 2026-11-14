"""Local MP4 range export using the bundled FFmpeg."""
import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg


@dataclass(frozen=True)
class LocalClipRequest:
    url: str  # Shared queue display field; a local source path, never a network URL.
    directory: str
    start: float
    end: float
    title: str = ""
    source_url: str = ""
    ranges: tuple[tuple[float, float], ...] = ()

    @property
    def intervals(self):
        return self.ranges or ((self.start, self.end),)

    def validate(self):
        source = Path(self.url)
        if not source.is_absolute() or not source.is_file() or source.suffix.lower() != ".mp4":
            raise ValueError("切り出し元のMP4が見つかりません。")
        if not self.directory.strip() or not Path(self.directory).is_absolute():
            raise ValueError("保存先は絶対パスで指定してください。")
        if len(self.intervals) > 100:
            raise ValueError("区間は100件以内にしてください。")
        for start, end in self.intervals:
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError("開始は0秒以上、終了は開始より後にしてください。")


def duration_of(path):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=flags, timeout=30, check=False)
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise ValueError("動画の長さを取得できません。MP4が正常か確認してください。")
    h, m, s = map(float, match.groups())
    return h * 3600 + m * 60 + s


def run_local_clip(request, emit):
    request.validate()
    source = Path(request.url)
    duration = duration_of(source)
    intervals = request.intervals
    total = sum(end - start for start, end in intervals)
    if any(start >= duration or end > duration + 0.05 for start, end in intervals):
        raise ValueError(f"指定範囲が動画の長さ（{duration:.2f}秒）を超えています。")
    root = Path(request.directory)
    if os.name == "nt" and not Path(root.anchor).exists():
        raise ValueError("保存先のドライブが接続されていません。")
    folder = root / (time.strftime("local-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    emit({"type": "folder", "path": str(folder)})
    label = (f"{request.start:g}-{request.end:g}s" if len(intervals) == 1
             else f"{len(intervals)}-sections")
    output = folder / f"{source.stem[:90]} [{label}].mp4"
    temporary = folder / "rendering.part.mp4"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def render(command, log, offset, scale=1):
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True,
                              encoding="utf-8", errors="replace", creationflags=flags) as process:
            for line in process.stdout:
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.strip().split("=", 1)[1]) / 1000000
                    except ValueError:
                        continue
                    emit({"type": "clip_progress",
                          "percent": min(99, (offset + seconds * scale) / total * 100)})
            if process.wait():
                raise RuntimeError("切り出しに失敗しました。保存先のffmpeg.logを確認してください。")

    base = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", "-n"]
    progress = ["-progress", "pipe:1", "-nostats"]
    try:
        with (folder / "ffmpeg.log").open("w", encoding="utf-8") as log, \
                tempfile.TemporaryDirectory(prefix="segments-", dir=folder) as work:
            parts = []
            elapsed = 0
            for index, (start, end) in enumerate(intervals):
                part = temporary if len(intervals) == 1 else Path(work) / f"part-{index:03d}.mkv"
                command = base + ["-ss", str(start), "-i", str(source), "-t", str(end-start),
                                  "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
                                  "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                                  "-c:a", "aac" if len(intervals) == 1 else "pcm_s16le"]
                if len(intervals) == 1:
                    command += ["-b:a", "192k", "-movflags", "+faststart"]
                render(command + progress + [str(part)], log, elapsed)
                elapsed += end-start
                parts.append(part)
            if len(parts) > 1:
                manifest = Path(work) / "concat.txt"
                manifest.write_text("".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8")
                render(base + ["-f", "concat", "-safe", "1", "-i", str(manifest),
                               "-map", "0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               "-movflags", "+faststart"]
                       + progress + [str(temporary)], log, total, 0)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("出力したMP4を確認できません。")
        temporary.rename(output)
        result = {"version": 1, "kind": "local_clip", "title": request.title or source.stem,
                  "source_url": request.source_url, "source_file": str(source),
                  "requested_range": list(intervals[0]) if len(intervals) == 1 else None,
                  "requested_ranges": [list(pair) for pair in intervals], "output": str(output),
                  "output_size": output.stat().st_size,
                  "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (folder / "clip.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        temporary.unlink(missing_ok=True)
