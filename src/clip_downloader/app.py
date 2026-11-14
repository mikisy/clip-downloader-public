"""Windows desktop application. Downloads run in a separate process."""

import argparse
import json
import multiprocessing as mp
import os
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import imageio_ffmpeg

from clip_downloader.analysis_history import STUDIO, AnalysisHistory
from clip_downloader.analysis_ui import AnalysisPanel
from clip_downloader.download import DownloadRequest, deno_path, parse_time
from clip_downloader.jobs import DownloadQueue
from clip_downloader.library import DownloadLibrary
from clip_downloader.search import VideoSearch


def settings_file():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClipDownloader/settings.json"


def load_settings():
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check_environment():
    import tkinter
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {}
    for name, binary in (("ffmpeg", imageio_ffmpeg.get_ffmpeg_exe()), ("deno", deno_path())):
        args = [binary, "-version" if name == "ffmpeg" else "--version"]
        output = subprocess.run(args, capture_output=True, text=True, check=True, creationflags=flags)
        result[name] = output.stdout.splitlines()[0]
    return {"python": platform.python_version(), "tk": tkinter.TkVersion,
            "yt_dlp": version("yt-dlp"), **result}



class Application:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.root = root
        self.closing = False
        saved = load_settings()
        self.directory = tk.StringVar(value=saved.get("directory", "D:\\Youtube"))
        self.quality = tk.StringVar(value=saved.get("quality", "1080p"))
        if self.quality.get() not in ("720p", "1080p", "最高画質"):
            self.quality.set("1080p")
        self.parallel = tk.StringVar(value=str(saved.get("parallel", 2)))
        if self.parallel.get() not in ("1", "2", "3", "4"):
            self.parallel.set("2")
        self.downloads = DownloadQueue(limit=int(self.parallel.get()))
        self.skip_existing = tk.BooleanVar(value=True)
        self.partial = tk.BooleanVar()
        self.start_time = tk.StringVar(value="00:00:00")
        self.end_time = tk.StringVar(value="00:00:30")
        self.status = tk.StringVar(value="URLを1行に1件入力してください。")
        root.title("Clip Downloader")
        root.geometry("960x780")
        root.minsize(780, 650)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        frame = ttk.Frame(self.notebook, padding=18)
        self.download_frame = frame
        self.notebook.add(frame, text="ダウンロード")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)
        ttk.Label(frame, text="Clip Downloader", font=("Segoe UI", 20)).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="動画URL\n1行に1件").grid(row=1, column=0, sticky="nw", padx=(0, 12))
        self.url_text = tk.Text(frame, height=4, wrap="none", undo=True)
        self.url_text.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(frame, text="保存先").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.directory).grid(row=2, column=1, sticky="ew")
        ttk.Button(frame, text="参照…", command=self.browse).grid(row=2, column=2, padx=(8, 0))
        options = ttk.Frame(frame)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(options, text="画質").pack(side="left", padx=(0, 8))
        ttk.Combobox(options, textvariable=self.quality, state="readonly",
                     values=("720p", "1080p", "最高画質"), width=10).pack(side="left")
        ttk.Label(options, text="同時DL").pack(side="left", padx=(18, 8))
        parallel = ttk.Combobox(options, textvariable=self.parallel, state="readonly",
                                values=("1", "2", "3", "4"), width=3)
        parallel.pack(side="left")
        parallel.bind("<<ComboboxSelected>>", lambda _: self.set_parallel())
        ttk.Checkbutton(options, text="取得済みをスキップ", variable=self.skip_existing).pack(
            side="left", padx=18)
        times = ttk.Frame(frame)
        times.grid(row=4, column=0, columnspan=3, sticky="ew")
        ttk.Checkbutton(times, text="指定区間のみ", variable=self.partial,
                        command=self.update_range).pack(side="left", padx=(0, 12))
        ttk.Label(times, text="開始").pack(side="left")
        self.start_entry = ttk.Entry(times, textvariable=self.start_time, width=12)
        self.start_entry.pack(side="left", padx=8)
        ttk.Label(times, text="終了").pack(side="left")
        self.end_entry = ttk.Entry(times, textvariable=self.end_time, width=12)
        self.end_entry.pack(side="left", padx=8)
        ttk.Label(times, text="時:分:秒（小数可）").pack(side="left")
        ttk.Label(frame, text="保存先内の完了情報と実ファイルで確認します。区間の境界は少しずれる場合があります。",
                  wraplength=860).grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=8)
        self.start_button = ttk.Button(actions, text="キューに追加・開始", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(actions, text="選択をキャンセル", command=self.cancel_selected).pack(
            side="left", padx=8)
        ttk.Button(actions, text="すべて停止", command=self.cancel).pack(side="left")
        ttk.Button(actions, text="保存フォルダを開く", command=self.open_folder).pack(side="right")
        table_frame = ttk.Frame(frame)
        table_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.table = ttk.Treeview(table_frame, columns=("video", "range", "progress"),
                                  show="headings", selectmode="extended", height=8)
        for key, title, width in (("video", "動画", 390), ("range", "区間", 130),
                                  ("progress", "状態・進捗", 300)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=80)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.log = tk.Text(frame, height=6, wrap="word", state="disabled")
        self.log.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=860).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.search = VideoSearch(self.notebook, self.add_search_urls, self.queue_search_urls)
        self.notebook.insert(0, self.search.frame)
        self.library = DownloadLibrary(self.notebook, self.directory, self.submit_clip,
                                       lambda record: self.analysis.open(record))
        self.analysis = AnalysisPanel(self.notebook, saved.get("analysis", {}), self.save_settings)
        self.analysis_history = AnalysisHistory(self.notebook, self.directory, saved.get("studio_wsl_path", STUDIO), self.save_settings)
        self.notebook.bind("<<NotebookTabChanged>>", self.tab_changed)
        self.update_range()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self.poll)
        self.url_text.focus_set()

    def add_search_urls(self, urls):
        if self.closing:
            return
        existing = self.url_text.get("1.0", "end").strip()
        known = set(existing.splitlines())
        added = [url for url in dict.fromkeys(urls) if url not in known]
        if added:
            self.url_text.insert("end-1c", ("\n" if existing else "") + "\n".join(added))
        self.notebook.select(self.download_frame)
        self.url_text.focus_set()

    def queue_search_urls(self, urls):
        if self.start(urls):
            self.notebook.select(self.download_frame)
            return True
        return False

    def submit_clip(self, request):
        if self.closing:
            raise ValueError("終了処理中です。")
        job = self.downloads.add(request)
        self.table.insert("", "end", iid=job.id)
        self.refresh_row(job)
        self.downloads.launch_pending()
        self.notebook.select(self.download_frame)

    def tab_changed(self, _event=None):
        if self.analysis_history.visible:
            self.analysis_history.reload()
        if self.library.visible:
            self.library.refresh()

    def update_range(self):
        for widget in (self.start_entry, self.end_entry):
            widget.configure(state="normal" if self.partial.get() else "disabled")

    def set_parallel(self):
        self.downloads.limit = int(self.parallel.get())
        self.save_settings()

    def save_settings(self):
        try:
            target = settings_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"directory": self.directory.get(),
                                             "quality": self.quality.get(),
                                             "parallel": self.downloads.limit,
                                             "analysis": self.analysis.saved,
                                             "studio_wsl_path": self.analysis_history.studio_location.get().strip()}, ensure_ascii=False),
                                 encoding="utf-8")
            temporary.replace(target)
            return True
        except OSError as error:
            self.append_log(f"設定を保存できませんでした: {error}")
            return False

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", str(text) + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 250:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def browse(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory()
        if folder:
            self.directory.set(folder)

    def open_folder(self):
        from tkinter import messagebox
        selected = set(self.table.selection())
        job = next((job for job in self.downloads.jobs if job.id in selected), None)
        path = Path(job.folder if job and job.folder else self.directory.get())
        try:
            if not path.is_dir():
                raise ValueError("保存先フォルダはまだ作成されていません。")
            os.startfile(str(path))
        except (OSError, ValueError) as error:
            messagebox.showerror("フォルダを開けません", str(error))

    def start(self, urls=None):
        from tkinter import messagebox
        if self.closing:
            return False
        from_input = urls is None
        try:
            if from_input:
                urls = [line.strip() for line in self.url_text.get("1.0", "end").splitlines()
                        if line.strip()]
            if not 1 <= len(urls) <= 100:
                raise ValueError("URLを1行に1件、1〜100件入力してください。")
            requests = [DownloadRequest(
                url, self.directory.get().strip(), self.quality.get(),
                parse_time(self.start_time.get()) if self.partial.get() else None,
                parse_time(self.end_time.get()) if self.partial.get() else None,
                self.skip_existing.get(),
            ) for url in urls]
            for request in requests:
                request.validate()
        except ValueError as error:
            messagebox.showerror("入力を確認してください", str(error))
            return False
        self.set_parallel()
        for request in requests:
            job = self.downloads.add(request)
            self.table.insert("", "end", iid=job.id)
            self.refresh_row(job)
        if from_input:
            self.url_text.delete("1.0", "end")
        self.downloads.launch_pending()
        return True

    def refresh_row(self, job):
        result = (job.terminal or {}).get("result", {})
        title = result.get("title") or job.request.url
        interval = "全編" if job.request.start is None else f"{job.request.start:g}〜{job.request.end:g}秒"
        ranges = getattr(job.request, "ranges", ())
        if len(ranges) > 1:
            interval = f"{len(ranges)}区間 / 計{sum(b-a for a, b in ranges):g}秒"
        self.table.item(job.id, values=(title, interval, job.progress))

    def handle_event(self, job, event):
        kind = event["type"]
        if kind in ("log", "error"):
            self.append_log(f"[{job.id}] {event['text']}")
        elif kind == "download_phase":
            job.progress = event["text"]
        elif kind == "clip_progress":
            job.progress = f"ローカル切り出し {event['percent']:.0f}%"
        elif kind == "progress":
            total = event.get("total")
            if event.get("status") == "downloading" and total:
                percent = event.get("downloaded", 0) / total * 100
                speed = (event.get("speed") or 0) / 1024 / 1024
                eta = event.get("eta")
                remaining = f"{int(eta)}秒" if eta is not None else "計算中"
                job.progress = f"{percent:.0f}% / {speed:.1f}MB/s / 残り{remaining}"
            else:
                job.progress = "取得・結合中…"
        elif kind == "finished":
            if job.state in ("done", "skipped"):
                self.library.invalidate()
            result = (job.terminal or {}).get("result", {})
            self.append_log(f"[{job.id}] {job.progress}: {result.get('output', '')}")
        self.refresh_row(job)

    def cancel_selected(self):
        selected = set(self.table.selection())
        for job in self.downloads.jobs:
            if job.id in selected:
                self.downloads.cancel(job)

    def cancel(self):
        self.downloads.cancel_all()

    def poll(self):
        self.analysis_history.poll()
        self.analysis.poll()
        self.search.poll()
        self.library.poll()
        self.downloads.poll(self.handle_event)
        for job in self.downloads.jobs:
            self.refresh_row(job)
        counts = {state: sum(j.state == state for j in self.downloads.jobs)
                  for state in ("pending", "running", "done", "skipped", "error", "cancelled")}
        self.status.set(f"待機 {counts['pending']} / 実行中 {counts['running']} / "
                        f"完了 {counts['done']} / 取得済み {counts['skipped']} / "
                        f"失敗 {counts['error']} / 停止 {counts['cancelled']}")
        if self.closing and not self.downloads.busy and not self.analysis.queue.busy:
            self.root.destroy()
        else:
            self.root.after(150, self.poll)

    def close(self):
        from tkinter import messagebox
        if self.downloads.busy or self.analysis.queue.busy:
            if not messagebox.askyesno("終了", "実行中の処理を停止して終了しますか？"):
                return
            self.closing = True
            self.start_button.configure(state="disabled")
            self.cancel()
            self.analysis.submit_button.configure(state="disabled")
            self.analysis.queue.cancel_all()
        else:
            self.root.destroy()


def main():
    mp.freeze_support()
    parser = argparse.ArgumentParser(description="Clip Downloader")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-analysis-device", choices=("cpu", "cuda", "auto"), default="cpu", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-analysis", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-search", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-player", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-download", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-directory", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.smoke_player:
        from clip_downloader.player import smoke_player
        sys.exit(0 if smoke_player(args.smoke_player) else 1)
    if args.check:
        print(json.dumps(check_environment(), ensure_ascii=False, indent=2))
        return
    import tkinter as tk
    root = tk.Tk()
    app = Application(root)
    smoke_ok = []
    if args.smoke_download:
        if not args.smoke_directory:
            parser.error("--smoke-directory is required")
        app.url_text.insert("1.0", args.smoke_download)
        app.directory.set(args.smoke_directory)
        app.start()

        def check_finished():
            if not app.downloads.busy:
                smoke_ok.append(bool(app.downloads.jobs) and all(
                    job.state in ("done", "skipped") for job in app.downloads.jobs))
                root.destroy()
            else:
                root.after(100, check_finished)
        root.after(100, check_finished)
    if args.smoke_analysis:
        import time
        deadline = time.monotonic() + 180
        app.analysis.open({"output": args.smoke_analysis})
        app.analysis.model.set("tiny")
        app.analysis.device.set(args.smoke_analysis_device)
        app.analysis.partial.set(True)
        app.analysis.start.set("1")
        app.analysis.end.set("9")
        app.analysis.save_settings = lambda: None
        app.analysis.submit()

        def check_analysis():
            if not app.analysis.queue.busy:
                smoke_ok.append(bool(app.analysis.queue.jobs) and all(
                    job.state == "done" and job.terminal["result"]["subtitle_count"] > 0
                    for job in app.analysis.queue.jobs))
                root.destroy()
            elif time.monotonic() > deadline:
                app.closing = True
                app.analysis.queue.cancel_all()
            else:
                root.after(100, check_analysis)
        root.after(100, check_analysis)
    if args.smoke_search:
        import time
        deadline = time.monotonic() + 60
        app.notebook.select(app.search.frame)
        app.search.query.set(args.smoke_search)
        app.search.start()

        def check_search():
            if app.search.rows and app.search.images:
                smoke_ok.append(True)
                root.destroy()
            elif (not app.search.busy and not app.search.rows) or time.monotonic() > deadline:
                root.destroy()
            else:
                root.after(100, check_search)
        root.after(100, check_search)
    if args.smoke:
        root.after(800, root.destroy)
    root.mainloop()
    if args.smoke_download or args.smoke_search or args.smoke_analysis:
        sys.exit(0 if smoke_ok and smoke_ok[0] else 1)


if __name__ == "__main__":
    main()
