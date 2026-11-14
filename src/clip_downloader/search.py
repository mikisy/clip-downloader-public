"""Metadata-only YouTube search and a nonblocking desktop search tab."""

import io
import math
import queue
import re
import threading
import urllib.request
import webbrowser
from dataclasses import dataclass
from tkinter import StringVar, messagebox, ttk

import yt_dlp
from PIL import Image, ImageTk

from clip_downloader.sources import SourceDialog
from clip_downloader.video_menu import VideoContextMenu


@dataclass(frozen=True)
class SearchResult:
    id: str
    title: str
    channel: str
    duration: float | None
    live_status: str | None = None

    @property
    def url(self):
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def duration_text(self):
        if self.live_status == "is_live":
            return "ライブ中"
        if self.live_status == "is_upcoming":
            return "配信予定"
        if self.duration is None:
            return "不明"
        seconds = int(self.duration)
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def search_videos(query, limit=20):
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError("検索キーワードを1〜200文字で入力してください。")
    if limit not in (10, 20, 50):
        raise ValueError("検索件数は10・20・50件から選択してください。")
    options = {"quiet": True, "no_warnings": True, "extract_flat": True,
               "skip_download": True, "cachedir": False, "socket_timeout": 12,
               "retries": 1, "extractor_retries": 1, "playlistend": limit}
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(f"ytsearch{limit}:{query}", download=False)
    rows = {}
    for entry in (info or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id", "")
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            continue
        duration = entry.get("duration")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            duration = None
        rows.setdefault(video_id, SearchResult(
            video_id, entry.get("title") or "（タイトル不明）",
            entry.get("channel") or entry.get("uploader") or "（チャンネル不明）",
            duration, entry.get("live_status")))
        if len(rows) >= limit:
            break
    return list(rows.values())


def thumbnail_image(video_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid video ID")
    url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    with urllib.request.urlopen(url, timeout=5) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("Thumbnail too large")
    with Image.open(io.BytesIO(data)) as image:
        image.thumbnail((120, 68))
        return image.convert("RGB")


class VideoSearch:
    def __init__(self, notebook, add_urls, queue_urls=None):
        self.add_urls = add_urls
        self.queue_urls = queue_urls
        self.frame = ttk.Frame(notebook, padding=18)
        notebook.add(self.frame, text="YouTube検索")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)
        self.query = StringVar()
        self.limit = StringVar(value="20")
        self.status = StringVar(value="キーワードで検索し、選択動画をダウンロード欄へ追加できます。")
        self.results = queue.SimpleQueue()
        self.rows = {}
        self.images = {}
        self.revision = 0
        self.cancelled = threading.Event()
        self.closed = False
        self.busy = False
        self.source_dialog = None
        controls = ttk.Frame(self.frame)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        controls.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(controls, textvariable=self.query)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda _: self.start())
        ttk.Combobox(controls, textvariable=self.limit, state="readonly",
                     values=("10", "20", "50"), width=4).grid(row=0, column=1, padx=8)
        self.search_button = ttk.Button(controls, text="検索", command=self.start)
        self.search_button.grid(row=0, column=2)
        actions = ttk.Frame(self.frame)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.add_button = ttk.Button(actions, text="選択動画をDL欄へ追加", command=self.add_selected,
                                     state="disabled")
        self.add_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="YouTubeで開く", command=self.open_selected,
                                      state="disabled")
        self.open_button.pack(side="left", padx=8)
        self.source_button = ttk.Button(actions, text="元動画URLを確認", command=self.show_sources,
                                        state="disabled")
        self.source_button.pack(side="left", padx=8)
        table_frame = ttk.Frame(self.frame)
        table_frame.grid(row=2, column=0, sticky="nsew")
        ttk.Style(self.frame).configure("Search.Treeview", rowheight=76)
        self.table = ttk.Treeview(table_frame, columns=("title", "channel", "duration"),
                                  show="tree headings", selectmode="extended",
                                  style="Search.Treeview")
        self.table.heading("#0", text="サムネイル")
        self.table.column("#0", width=135, minwidth=135, stretch=False)
        for key, title, width in (("title", "動画タイトル", 430),
                                  ("channel", "チャンネル", 190), ("duration", "再生時間", 95)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=80, stretch=key == "title")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.context_menu = VideoContextMenu(
            self.table, self.queue_selected, self.add_selected,
            self.show_sources, self.open_selected, queue_urls is not None)
        self.table.bind("<<TreeviewSelect>>", self.selection_changed)
        self.table.bind("<Return>", lambda _: self.add_selected())
        self.table.bind("<Double-Button-1>", self.double_click)
        ttk.Label(self.frame, textvariable=self.status, wraplength=850).grid(
            row=3, column=0, sticky="w", pady=10)
        ttk.Label(self.frame, text="Ctrl / Shiftで複数選択。ダブルクリックでYouTubeを開きます。"
                  "追加後、画質や区間を指定してDLを開始してください。", wraplength=850).grid(
            row=4, column=0, sticky="w")
        self.frame.bind("<Destroy>", self.destroyed)

    def destroyed(self, event):
        if event.widget == self.frame:
            self.closed = True
            self.cancelled.set()

    def start(self):
        if self.closed or self.busy:
            return
        query = self.query.get().strip()
        if not query or len(query) > 200:
            self.status.set("検索キーワードを1〜200文字で入力してください。")
            return
        self.cancelled.set()
        self.cancelled = threading.Event()
        self.revision += 1
        revision, stop, limit = self.revision, self.cancelled, int(self.limit.get())
        self.busy = True
        self.search_button.configure(state="disabled")
        self.table.delete(*self.table.get_children())
        self.rows.clear()
        self.images.clear()
        self.selection_changed()
        self.status.set(f"「{query}」を検索中…")

        def run():
            try:
                rows = search_videos(query, limit)
                if stop.is_set():
                    return
                self.results.put((revision, "rows", rows))
                for row in rows:
                    if stop.is_set():
                        return
                    try:
                        image = thumbnail_image(row.id)
                    except (OSError, ValueError, Image.DecompressionBombError):
                        image = None  # The video remains selectable without its thumbnail.
                    if image is not None and not stop.is_set():
                        self.results.put((revision, "image", (row.id, image)))
            except (yt_dlp.utils.DownloadError, OSError, ValueError, RuntimeError) as error:
                if not stop.is_set():
                    self.results.put((revision, "error", str(error)))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        for _ in range(60):
            try:
                revision, kind, value = self.results.get_nowait()
            except queue.Empty:
                break
            if revision != self.revision:
                continue
            if kind in ("rows", "error"):
                self.busy = False
                self.search_button.configure(state="normal")
            if kind == "rows":
                self.rows = {row.id: row for row in value}
                for row in value:
                    self.table.insert("", "end", iid=row.id, values=(
                        row.title, row.channel, row.duration_text))
                self.status.set(f"{len(value)}件見つかりました。" if value else
                                "動画が見つかりませんでした。キーワードを変えてお試しください。")
            elif kind == "error":
                self.status.set(f"検索に失敗しました。再検索できます：{value[:250]}")
            elif kind == "image" and value[0] in self.rows:
                video_id, image = value
                self.images[video_id] = ImageTk.PhotoImage(image, master=self.frame)
                self.table.item(video_id, image=self.images[video_id])

    def selection_changed(self, _event=None):
        count = len(self.table.selection())
        self.add_button.configure(state="normal" if count else "disabled")
        self.open_button.configure(state="normal" if count == 1 else "disabled")
        self.source_button.configure(state="normal" if count == 1 else "disabled")

    def show_sources(self):
        selected = self.table.selection()
        if len(selected) != 1 or selected[0] not in self.rows:
            return
        if self.source_dialog and not self.source_dialog.closed:
            if self.source_dialog.video.id == selected[0]:
                self.source_dialog.window.lift()
                return
            self.source_dialog.window.destroy()
        self.source_dialog = SourceDialog(self.frame, self.rows[selected[0]], self.add_urls, self.queue_urls)

    def add_selected(self):
        urls = [self.rows[key].url for key in self.table.selection() if key in self.rows]
        if urls:
            self.add_urls(urls)

    def queue_selected(self):
        urls = [self.rows[key].url for key in self.table.selection() if key in self.rows]
        if urls and self.queue_urls:
            self.queue_urls(urls)

    def double_click(self, event):
        if self.table.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        row = self.table.identify_row(event.y)
        if row:
            self.table.selection_set(row)
            self.open_selected()

    def open_selected(self):
        selected = self.table.selection()
        if len(selected) == 1 and selected[0] in self.rows:
            try:
                if not webbrowser.open(self.rows[selected[0]].url):
                    raise OSError("既定のブラウザを起動できませんでした。")
            except OSError as error:
                messagebox.showerror("動画を開けません", str(error), parent=self.frame)
