"""Local Whisper analysis. All subtitle times refer to the selected file, not the crop."""
import ctypes
import importlib.util
import json
import math
import os
import re
import subprocess
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg

from clip_downloader.analysis_media import prepare_analysis_media
from clip_downloader.local_clip import duration_of

MODELS = ("large-v3", "large-v3-turbo", "medium", "small", "base", "tiny")
DEFAULT_CORRECTIONS = {"ミオシャ": "ミオしゃ"}


def parse_corrections(text):
    rules = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if "=>" not in line:
            raise ValueError("補正は1行に「置換前 => 置換後」で入力してください。")
        before, after = (part.strip() for part in line.split("=>", 1))
        if not before or before in rules:
            raise ValueError("補正の置換前は空欄・重複にできません。")
        rules[before] = after
    return rules


def correct_text(text, rules):
    # Longest match first; replacements must not trigger another rule.
    if not rules:
        return text
    pattern = "|".join(re.escape(key) for key in sorted(rules, key=len, reverse=True))
    return re.sub(pattern, lambda match: rules[match.group()], text)


@dataclass(frozen=True)
class AnalysisRequest:
    url: str  # Local file path, matching the existing cancellable queue interface.
    start: float | None = None
    end: float | None = None
    model: str = "large-v3"
    device: str = "auto"
    hotwords: tuple[str, ...] = ()
    corrections: dict = field(default_factory=lambda: dict(DEFAULT_CORRECTIONS))
    source: dict = field(default_factory=dict)

    def validate(self):
        path = Path(self.url)
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".mp4":
            raise ValueError("解析するMP4を選択してください。")
        if self.model not in MODELS or self.device not in ("auto", "cpu", "cuda"):
            raise ValueError("モデル・実行先を選び直してください。")
        if (self.start is None) != (self.end is None):
            raise ValueError("開始と終了を両方指定してください。")
        if self.start is not None and (not math.isfinite(self.start) or not math.isfinite(self.end)
                                      or self.start < 0 or self.end <= self.start):
            raise ValueError("開始は0秒以上、終了は開始より後にしてください。")
        if any(not isinstance(k, str) or not k or not isinstance(v, str)
               for k, v in self.corrections.items()):
            raise ValueError("補正ルールを確認してください。")


# Keep DLL-directory handles alive for libraries loaded lazily during inference.
_cuda_directories = {}
_cuda_libraries = []


def prepare_cuda_dlls():
    if os.name != "nt":
        return {}
    paths = []
    for package in ("nvidia.cuda_runtime", "nvidia.cuda_nvrtc", "nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(package)
        except ModuleNotFoundError:
            continue
        for location in (spec.submodule_search_locations or []) if spec else []:
            for folder in (Path(location) / "bin", Path(location) / "lib", Path(location)):
                if not folder.is_dir() or not any(folder.glob("*.dll")):
                    continue
                paths.append(folder)
                key = str(folder.resolve())
                if key not in _cuda_directories:
                    _cuda_directories[key] = os.add_dll_directory(key)
                    os.environ["PATH"] = key + os.pathsep + os.environ.get("PATH", "")
    return {file.name.lower(): file for folder in paths for file in folder.glob("*.dll")}


def select_device(requested, emit):
    if requested == "cpu":
        return "cpu"
    try:
        dlls = prepare_cuda_dlls()
        import ctranslate2
        if not ctranslate2.get_cuda_device_count():
            raise RuntimeError("NVIDIA GPUが見つかりません。")
        if os.name == "nt":
            # Driver detection alone does not prove that inference DLLs are available.
            for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"):
                _cuda_libraries.append(ctypes.WinDLL(str(dlls.get(name.lower(), name))))
        available = True
    except (RuntimeError, OSError) as error:
        available = False
        reason = str(error)
    if requested == "cuda" and not available:
        raise RuntimeError(f"GPUを使用できません。CUDA 12 / cuDNN 9のDLLを確認するかCPUを選んでください。{reason}")
    device = "cuda" if requested != "cpu" and available else "cpu"
    if requested == "auto" and not available:
        emit({"type": "log", "text": f"GPU環境を利用できないためCPUで解析します: {reason}"})
    return device


def timestamp(seconds):
    ms = max(0, round(seconds * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def save_result(folder, data):
    srt = "\n\n".join(f"{i}\n{timestamp(row['start'])} --> {timestamp(row['end'])}\n{row['text']}"
                      for i, row in enumerate(data["segments"], 1))
    (folder / "subtitles.srt").write_text(srt + ("\n" if srt else ""), encoding="utf-8")
    # The JSON is the completion marker. Interrupted jobs never have a completed JSON.
    temporary = folder / "analysis.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(folder / "analysis.json")


def read_audio(path):
    import numpy as np
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != 16000 or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("抽出音声の形式が不正です。")
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def run_analysis(request, emit):
    request.validate()
    started_clock = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    source = Path(request.url).resolve()
    stat = source.stat()
    duration = duration_of(source)
    start = request.start or 0.0
    end = duration if request.end is None else request.end
    if start >= duration or end > duration + 0.05:
        raise ValueError(f"指定範囲が動画の長さ（{duration:.2f}秒）を超えています。")
    end = min(end, duration)
    identity = uuid.uuid4().hex
    folder = source.parent / "analysis" / (time.strftime("%Y%m%d-%H%M%S-") + identity[:8])
    folder.mkdir(parents=True)
    emit({"type": "folder", "path": str(folder)})
    audio = folder / "audio.part.wav"
    try:
        playback_source = prepare_analysis_media(source, emit)
        playback_stat = playback_source.stat()
        emit({"type": "analysis_progress", "text": "AAC動画から指定範囲の音声を抽出中…"})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-v", "error",
                   "-ss", str(start), "-i", str(playback_source), "-t", str(end - start), "-vn",
                   "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)]
        with (folder / "ffmpeg.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=log, creationflags=flags, check=False)
        if result.returncode:
            raise RuntimeError("音声抽出に失敗しました。音声の有無とffmpeg.logを確認してください。")
        device = select_device(request.device, emit)
        compute_type = "float16" if device == "cuda" else "int8"
        emit({"type": "analysis_progress", "text": f"{device.upper()} / {request.model} 読込中（初回はモデルDL）…"})
        from faster_whisper import WhisperModel
        cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClipDownloader/models"
        model = WhisperModel(request.model, device=device, compute_type=compute_type,
                             download_root=str(cache), cpu_threads=min(8, os.cpu_count() or 4))
        # Preserve the original audio clock. VAD concatenation can shift word
        # alignment around music, laughter and overlapping speech. Do not
        # remove samples from the timeline before recognition.
        segments, info = model.transcribe(read_audio(audio), language="ja", beam_size=5,
                                          word_timestamps=True, vad_filter=False,
                                          condition_on_previous_text=False,
                                          hotwords=", ".join(request.hotwords) or None)
        rows = []
        for segment in segments:
            a, b = max(start, start + segment.start), min(end, start + segment.end)
            if b <= a:
                continue
            raw = segment.text.strip()
            words = [{"start": max(a, start + w.start), "end": min(b, start + w.end),
                      "text": w.word, "probability": w.probability}
                     for w in (segment.words or []) if start + w.end > a and start + w.start < b]
            rows.append({"id": f"{identity}-{len(rows)+1:06}", "start": a, "end": b,
                         "raw_text": raw, "text": correct_text(raw, request.corrections),
                         "speaker": None, "words": words})
            emit({"type": "analysis_progress", "text": f"{device.upper()} 文字起こし {min(99, (b-start)/(end-start)*100):.0f}% / {len(rows)}件"})
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("解析中に元動画が変更されました。結果は確定していません。")
        completed_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        elapsed_seconds = round(time.monotonic() - started_clock, 3)
        data = {"schema_version": 1, "id": identity, "kind": "whisper_analysis",
                "created_at": completed_at, "completed_at": completed_at,
                "started_at": started_at, "elapsed_seconds": elapsed_seconds,
                "source_file": str(playback_source), "source_size": playback_stat.st_size,
                "source_mtime_ns": playback_stat.st_mtime_ns,
                "original_source_file": str(source), "original_source_size": stat.st_size,
                "original_source_mtime_ns": stat.st_mtime_ns,
                "playback_audio_policy": "aac_shared_source_v1", "source_metadata": request.source,
                "time_basis": "source_file_seconds", "analyzed_range": [start, end],
                "source_duration": duration, "language": info.language,
                "model": request.model, "device": device, "compute_type": compute_type,
                "hotwords": list(request.hotwords), "corrections": request.corrections,
                "word_text_basis": "raw_recognition",
                "timing_policy": "original_audio_clock_v1",
                "vad_filter": False,
                "segments": rows}
        save_result(folder, data)
        return {"title": source.stem, "output": str(folder / "analysis.json"),
                "subtitle_count": len(rows), "created_at": completed_at,
                "completed_at": completed_at, "started_at": started_at,
                "elapsed_seconds": elapsed_seconds}
    except Exception as error:
        (folder / "error.txt").write_text(str(error), encoding="utf-8")
        raise
    finally:
        audio.unlink(missing_ok=True)


def analysis_worker(request, events):
    try:
        events.put({"type": "done", "result": run_analysis(request, events.put)})
    except Exception as error:  # noqa: BLE001 - serialize worker failure to the UI
        events.put({"type": "error", "text": str(error)})
