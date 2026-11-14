"""Bounded worker queue; each download has its own cancellation and events."""

import multiprocessing as mp
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field

from clip_downloader.download import worker

TERMINAL = {"done", "skipped", "error", "cancelled"}


@dataclass
class Job:
    request: object
    id: str
    state: str = "pending"
    process: object = None
    events: object = None
    terminal: dict | None = None
    folder: str | None = None
    progress: str = "待機中"
    dead_polls: int = 0
    stopping: bool = False
    stop_thread: object = None
    stop_error: str | None = None


@dataclass
class DownloadQueue:
    limit: int = 2
    jobs: list = field(default_factory=list)
    worker_target: object = worker

    @property
    def active(self):
        return [job for job in self.jobs if job.process is not None]

    @property
    def busy(self):
        return any(job.state not in TERMINAL for job in self.jobs)

    def add(self, request):
        request.validate()
        job = Job(request, str(len(self.jobs) + 1))
        self.jobs.append(job)
        return job

    def launch_pending(self):
        slots = max(0, max(1, min(4, self.limit)) - len(self.active))
        for job in self.jobs:
            if slots == 0:
                break
            if job.state != "pending":
                continue
            ctx = mp.get_context("spawn")
            job.events = ctx.Queue()
            job.process = ctx.Process(target=self.worker_target, args=(job.request, job.events))
            try:
                job.process.start()
            except (OSError, RuntimeError) as error:
                job.events.close()
                job.process = None
                job.state = "error"
                job.terminal = {"type": "error", "text": str(error)}
                job.progress = str(error)
                continue
            job.state = "running"
            job.progress = "処理を起動中…"
            slots -= 1

    def cancel(self, job):
        if job.state == "pending":
            job.state = "cancelled"
            job.progress = "キャンセル"
            return
        if job.process is None or job.stopping or job.terminal:
            return
        job.stopping = True
        job.progress = "停止中…"
        process = job.process

        def stop():
            try:
                if process.is_alive():
                    if os.name == "nt":
                        result = subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        if result.returncode and process.is_alive():
                            job.stop_error = "停止できませんでした。もう一度キャンセルしてください。"
                    else:
                        process.terminate()
            except OSError as error:
                job.stop_error = str(error)
        job.stop_thread = threading.Thread(target=stop, daemon=True)
        job.stop_thread.start()

    def cancel_all(self):
        for job in self.jobs:
            self.cancel(job)

    def poll(self, emit):
        for job in self.active:
            if job.stop_error and not job.stop_thread.is_alive():
                job.progress = job.stop_error
                emit(job, {"type": "log", "text": job.stop_error})
                job.stop_error = None
                job.stopping = False
            # Terminating a queue writer can leave a partial message. Discard its queue.
            if not job.stopping:
                for _ in range(100):
                    try:
                        event = job.events.get_nowait()
                    except queue.Empty:
                        break
                    kind = event["type"]
                    if kind in ("done", "error"):
                        job.terminal = event
                    elif kind == "folder":
                        job.folder = event["path"]
                    emit(job, event)
            if not job.process.is_alive() and not (job.stop_thread and job.stop_thread.is_alive()):
                job.dead_polls += 1
                if job.terminal or job.stopping or job.dead_polls >= 4:
                    job.process.join()
                    job.process.close()
                    job.process = None
                    job.events.close()
                    job.events = None
                    if job.stopping:
                        job.state, job.progress = "cancelled", "キャンセル"
                    elif job.terminal and job.terminal["type"] == "done":
                        skipped = job.terminal["result"].get("skipped", False)
                        job.state = "skipped" if skipped else "done"
                        job.progress = "取得済み・スキップ" if skipped else "完了"
                    else:
                        job.state = "error"
                        job.progress = (job.terminal or {}).get("text", "処理が予期せず終了しました。")
                    emit(job, {"type": "finished"})
        self.launch_pending()
