"""Analysis tab; native UI stays responsive while the worker owns Whisper."""
import os
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, filedialog, messagebox, ttk

from clip_downloader.analysis import (
    DEFAULT_CORRECTIONS,
    MODELS,
    AnalysisRequest,
    analysis_worker,
    parse_corrections,
)
from clip_downloader.download import parse_time
from clip_downloader.jobs import DownloadQueue


class AnalysisPanel:
    def __init__(self, notebook, saved, save_settings):
        self.notebook = notebook
        self.save_settings = save_settings
        self.saved = saved if isinstance(saved, dict) else {}
        self.queue = DownloadQueue(limit=1, worker_target=analysis_worker)
        self.source = {}
        self.frame = ttk.Frame(notebook, padding=18)
        notebook.add(self.frame, text="文字起こし・解析")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(6, weight=1)
        self.path = StringVar()
        self.partial = BooleanVar()
        self.start = StringVar(value="00:00:00")
        self.end = StringVar(value="00:00:30")
        self.model = StringVar(value=self.saved.get("model", "large-v3"))
        self.device = StringVar(value=self.saved.get("device", "auto"))
        if self.model.get() not in MODELS:
            self.model.set("large-v3")
        if self.device.get() not in ("auto", "cuda", "cpu"):
            self.device.set("auto")
        self.status = StringVar(value="MP4を選び、全編または指定範囲を解析します。話者分離は行いません。")
        line = ttk.Frame(self.frame)
        line.grid(row=0, column=0, sticky="ew")
        line.columnconfigure(0, weight=1)
        ttk.Entry(line, textvariable=self.path, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(line, text="MP4を選択", command=self.browse).grid(row=0, column=1, padx=8)
        times = ttk.Frame(self.frame)
        times.grid(row=1, column=0, sticky="w", pady=10)
        ttk.Checkbutton(times, text="指定範囲のみ", variable=self.partial,
                        command=self.update_range).pack(side="left")
        self.time_entries = []
        for label, var in (("開始", self.start), ("終了", self.end)):
            ttk.Label(times, text=label).pack(side="left", padx=8)
            entry = ttk.Entry(times, textvariable=var, width=14)
            entry.pack(side="left")
            self.time_entries.append(entry)
        ttk.Label(times, text="ファイル先頭からの時:分:秒").pack(side="left", padx=8)
        options = ttk.Frame(self.frame)
        options.grid(row=2, column=0, sticky="w", pady=8)
        for label, var, values in (("Whisper", self.model, MODELS),
                                   ("実行先", self.device, ("auto", "cuda", "cpu"))):
            ttk.Label(options, text=label).pack(side="left", padx=(0, 8))
            ttk.Combobox(options, textvariable=var, values=values, state="readonly", width=16).pack(
                side="left", padx=(0, 16))
        ttk.Label(options, text="auto: GPU環境がなければCPU").pack(side="left")
        dictionaries = ttk.Frame(self.frame)
        dictionaries.grid(row=3, column=0, sticky="ew", pady=8)
        dictionaries.columnconfigure((0, 1), weight=1)
        ttk.Label(dictionaries, text="認識のヒント（1行1語）").grid(row=0, column=0, sticky="w")
        ttk.Label(dictionaries, text="認識後の補正（置換前 => 置換後）").grid(row=0, column=1, sticky="w")
        self.hotwords = Text(dictionaries, height=4, width=30)
        self.corrections = Text(dictionaries, height=4, width=30)
        self.hotwords.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.corrections.grid(row=1, column=1, sticky="ew")
        self.hotwords.insert("1.0", "\n".join(self.saved.get("hotwords", [])))
        self.corrections.insert("1.0", "\n".join(f"{a} => {b}" for a, b in
                                                self.saved.get("corrections", DEFAULT_CORRECTIONS).items()))
        ttk.Label(self.frame, text="初回はモデルを取得します。解析結果は動画と同じフォルダの analysis 内に別々に保存します。",
                  wraplength=850).grid(row=4, column=0, sticky="w", pady=8)
        actions = ttk.Frame(self.frame)
        actions.grid(row=5, column=0, sticky="ew", pady=8)
        self.save_button = ttk.Button(actions, text="設定を保存", command=self.save_configuration)
        self.save_button.pack(side="left", padx=(0, 8))
        self.submit_button = ttk.Button(actions, text="解析キューに追加", command=self.submit)
        self.submit_button.pack(side="left")
        ttk.Button(actions, text="選択を停止", command=self.cancel_selected).pack(side="left", padx=8)
        ttk.Button(actions, text="結果フォルダ", command=self.open_result).pack(side="right")
        table_frame = ttk.Frame(self.frame)
        table_frame.grid(row=6, column=0, sticky="nsew")
        self.table = ttk.Treeview(table_frame, columns=("video", "range", "status", "elapsed", "completed"), show="headings")
        for key, label, width in (("video", "動画", 220), ("range", "範囲（秒）", 95),
                                   ("status", "進捗", 235), ("elapsed", "解析時間", 95),
                                   ("completed", "完了日時（作成日時）", 165)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=width if key in ("elapsed", "completed") else 70)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Double-Button-1>", lambda _: self.open_result())
        ttk.Label(self.frame, textvariable=self.status, wraplength=850).grid(row=7, column=0, sticky="w", pady=8)
        self.update_range()

    def open(self, record):
        self.source = dict(record)
        self.path.set(record["output"])
        self.partial.set(False)
        self.update_range()
        self.notebook.select(self.frame)

    def browse(self):
        path = filedialog.askopenfilename(parent=self.frame, filetypes=[("MP4", "*.mp4")])
        if path:
            self.open({"output": path})

    def update_range(self):
        for entry in self.time_entries:
            entry.configure(state="normal" if self.partial.get() else "disabled")

    def read_configuration(self):
        if self.model.get() not in MODELS or self.device.get() not in ("auto", "cuda", "cpu"):
            raise ValueError("モデル・実行先を選び直してください。")
        return {"model": self.model.get(), "device": self.device.get(),
                "hotwords": list(dict.fromkeys(s.strip() for s in
                                 self.hotwords.get("1.0", "end").splitlines() if s.strip())),
                "corrections": parse_corrections(self.corrections.get("1.0", "end"))}

    def persist_configuration(self, configuration):
        previous = self.saved
        self.saved = configuration
        if self.save_settings() is False:
            self.saved = previous
            return False
        return True

    def save_configuration(self):
        try:
            configuration = self.read_configuration()
            if not self.persist_configuration(configuration):
                self.status.set("設定を保存できませんでした。ダウンロード画面のログを確認してください。")
                return
        except (ValueError, OSError) as error:
            messagebox.showerror("設定を保存できません", str(error), parent=self.frame)
            return
        self.status.set("設定を保存しました。次回起動時も使用します。追加済みの解析には影響しません。")

    def submit(self):
        try:
            configuration = self.read_configuration()
            request = AnalysisRequest(
                self.path.get(), parse_time(self.start.get()) if self.partial.get() else None,
                parse_time(self.end.get()) if self.partial.get() else None,
                configuration["model"], configuration["device"],
                tuple(configuration["hotwords"]), dict(configuration["corrections"]), dict(self.source))
            request.validate()
            if any(job.request == request and job.state in ("pending", "running") for job in self.queue.jobs):
                self.status.set("同じ解析はすでにキューにあります。")
                return
            job = self.queue.add(request)
        except (ValueError, OSError) as error:
            messagebox.showerror("解析を開始できません", str(error), parent=self.frame)
            return
        if not self.persist_configuration(configuration):
            self.status.set("解析は開始しますが、次回用の設定保存に失敗しました。ログを確認してください。")
        self.table.insert("", "end", iid=job.id)
        self.queue.launch_pending()
        self.refresh(job)

    def refresh(self, job):
        request = job.request
        interval = "全編" if request.start is None else f"{request.start:g}〜{request.end:g}"
        result = (job.terminal or {}).get("result", {}) if job.state == "done" else {}
        elapsed = result.get("elapsed_seconds")
        elapsed_text = "—"
        if elapsed is not None:
            seconds = max(0, round(elapsed))
            elapsed_text = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        completed_text = "—"
        completed = result.get("completed_at") or result.get("created_at")
        if completed:
            try:
                completed_text = datetime.fromisoformat(completed).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        self.table.item(job.id, values=(Path(request.url).name, interval, job.progress,
                                       elapsed_text, completed_text))

    def event(self, job, event):
        if event["type"] == "analysis_progress":
            job.progress = event["text"]
        elif event["type"] in ("error", "log"):
            self.status.set(event["text"])
        elif event["type"] == "finished":
            result = (job.terminal or {}).get("result", {})
            if job.state == "done":
                job.progress = f"完了 / 字幕 {result['subtitle_count']}件"
                self.status.set(f"保存しました: {result['output']}")
            else:
                self.status.set(job.progress)
        self.refresh(job)

    def poll(self):
        self.queue.poll(self.event)
        for job in self.queue.jobs:
            self.refresh(job)

    def cancel_selected(self):
        for job in self.queue.jobs:
            if job.id in self.table.selection():
                self.queue.cancel(job)

    def open_result(self):
        selected = self.table.selection()
        job = next((j for j in self.queue.jobs if j.id in selected), None)
        if not job or not job.folder:
            self.status.set("解析の行を選択してください。結果は動画フォルダ内の analysis に残ります。")
            return
        try:
            os.startfile(job.folder)
        except OSError as error:
            messagebox.showerror("フォルダを開けません", str(error), parent=self.frame)
