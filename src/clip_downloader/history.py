"""Completed-file lookup and OS-owned cross-process download locks."""

import errno
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit


def source_key(url):
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    video_id = None
    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            video_id = parsed.path.split("/")[2]
    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return "youtube:" + video_id
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                       parsed.query, ""))


def request_key(request):
    return (source_key(request.url), request.start, request.end)


def completed_records(root):
    """Yield valid completed downloads, sharing validation with duplicate detection."""
    for metadata in Path(root).rglob("clip.json"):
        try:
            record = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                continue
            stored = Path(record["output"])
            # Prefer the file beside metadata when a download folder has been moved.
            for output in (metadata.parent / stored.name, stored):
                if output.suffix.lower() != ".mp4" or not output.is_file():
                    continue
                stat = output.stat()
                if stat.st_size <= 0:
                    continue
                size = record.get("output_size")
                if size is not None and stat.st_size != size:
                    continue
                yield {**record, "output": str(output), "file_size": stat.st_size,
                       "modified_at": stat.st_mtime}
                break
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def find_existing(request):
    wanted = request_key(request)
    for record in completed_records(request.directory):
        if record.get("kind") == "local_clip":
            continue
        prior = record.get("request")
        prior = prior if isinstance(prior, dict) else {}
        url = prior.get("url") or record.get("source_url")
        if not isinstance(url, str):
            continue
        ranges = record.get("requested_range", [prior.get("start"), prior.get("end")])
        if ranges == list(wanted[1:]) and source_key(url) == wanted[0]:
            return {**record, "skipped": True}
    return None


@contextmanager
def download_lock(request, emit):
    root = Path(request.directory) / ".clip-downloader" / "locks"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(request_key(request)).encode()).hexdigest()
    # The file stays, but the OS lock is released even if a worker is killed.
    with (root / f"{digest}.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        acquired = False
        waiting = False
        try:
            while not acquired:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not waiting:
                        emit({"type": "log", "text": "同じ動画・区間の取得完了を待っています。"})
                        waiting = True
                    time.sleep(0.2)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
