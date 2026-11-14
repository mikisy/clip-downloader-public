"""Reusable AAC playback source shared by recognition and Studio."""
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import imageio_ffmpeg


def prepare_analysis_media(source, emit):
    source = Path(source).resolve()
    stat = source.stat()
    identity = {'version': 1, 'path': str(source), 'size': stat.st_size,
                'mtime_ns': stat.st_mtime_ns}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    folder = source.parent / 'analysis-media' / key
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / source.name
    marker = folder / 'aac-source.json'
    try:
        saved = json.loads(marker.read_text(encoding='utf-8'))
        cached = output.stat()
        if (saved['source'] == identity and saved['size'] == cached.st_size
                and saved['mtime_ns'] == cached.st_mtime_ns and cached.st_size > 0):
            emit({'type': 'analysis_progress', 'text': 'AAC変換済み動画を再利用…'})
            return output
    except (OSError, ValueError, KeyError, TypeError):
        pass
    emit({'type': 'analysis_progress', 'text': '解析・Studio共用のAAC音声動画を準備中（映像は再圧縮しません）…'})
    token = uuid.uuid4().hex
    temporary = folder / f'{token}.part.mp4'
    temporary_marker = folder / f'{token}.part.json'
    try:
        command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-v', 'error',
                   '-i', str(source), '-map', '0:v:0', '-map', '0:a:0',
                   '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
                   str(temporary)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (folder / f'{token}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=log,
                                    creationflags=flags, check=False)
        if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError(f'AAC動画の準備に失敗しました。ログ: {folder / (token + ".log")}')
        current = source.stat()
        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError('AAC変換中に元動画が変更されました。再実行してください。')
        temporary.replace(output)
        cached = output.stat()
        temporary_marker.write_text(json.dumps({'source': identity, 'size': cached.st_size,
                                                'mtime_ns': cached.st_mtime_ns}), encoding='utf-8')
        temporary_marker.replace(marker)
        return output
    finally:
        temporary.unlink(missing_ok=True)
        temporary_marker.unlink(missing_ok=True)
