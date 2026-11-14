"""Downloaded-video tab, with background scanning and default-player launch."""

import ctypes
import os
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from tkinter import Menu, StringVar, Toplevel, messagebox, ttk

from clip_downloader.download import parse_time
from clip_downloader.history import completed_records
from clip_downloader.local_clip import LocalClipRequest
from clip_downloader.player import EmbeddedPlayer
from clip_downloader.preview import PreviewServer


def recycle_file(path, owner=0):
    """Recycle only the selected video; keep metadata so restored files reappear."""
    from ctypes import wintypes

    class FileOperation(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise OSError("削除対象の動画が見つかりません。")
    source = ctypes.create_unicode_buffer(str(path) + "\0")
    # ALLOWUNDO + WANTNUKEWARNING: Windows asks before deleting permanently
    # when the destination volume has no recycle bin.
    operation = FileOperation(hwnd=owner, wFunc=3, pFrom=ctypes.cast(source, wintypes.LPCWSTR),
                              fFlags=0x0040 | 0x4000)
    function = ctypes.windll.shell32.SHFileOperationW
    function.argtypes = [ctypes.POINTER(FileOperation)]
    function.restype = ctypes.c_int
    result = function(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError("削除がキャンセルされたか、ごみ箱へ移動できませんでした。")


def list_downloads(directory):
    root = Path(directory)
    if not directory.strip() or not root.is_absolute() or not root.is_dir():
        raise ValueError("ダウンロード画面で、有効な保存先フォルダを指定してください。")
    unique = {}
    for record in completed_records(root):
        key = str(Path(record["output"]).resolve()).casefold()
        unique.setdefault(key, record)
    return sorted(unique.values(), key=lambda row: row["modified_at"], reverse=True)


def interval_text(record):
    ranges = record.get("requested_ranges")
    if isinstance(ranges, list) and len(ranges) > 1:
        return f"{len(ranges)}区間 / 計{sum(b-a for a, b in ranges):g}秒"
    interval = record.get("requested_range")
    if interval == [None, None]:
        return "全編"
    if isinstance(interval, list) and len(interval) == 2:
        try:
            return f"{float(interval[0]):g}〜{float(interval[1]):g}秒"
        except (TypeError, ValueError):
            pass
    return "不明"


class DownloadLibrary:
    def __init__(self, notebook, directory, submit_clip, analyze=None):
        self.analyze = analyze
        self.submit_clip = submit_clip
        self.preview = None
        self.players = []
        self.root = notebook.winfo_toplevel()
        self.notebook = notebook
        self.directory = directory
        self.frame = ttk.Frame(notebook, padding=18)
        notebook.add(self.frame, text="ダウンロード済み")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)
        self.status = StringVar(value="保存先内の、このアプリで取得した動画を表示します。")
        self.search = StringVar()
        self.records = []
        self.rows = {}
        self.revision = 0
        self.dirty = True
        self.scanning = False
        self.results = queue.SimpleQueue()
        self.last_open = (None, 0.0)
        ttk.Label(self.frame, text="ダウンロード済み", font=("Segoe UI", 18)).grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(self.frame, textvariable=directory, wraplength=860).grid(
            row=1, column=0, sticky="w", pady=(0, 10))
        actions = ttk.Frame(self.frame)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(actions, text="検索").pack(side="left", padx=(0, 8))
        ttk.Entry(actions, textvariable=self.search, width=30).pack(side="left")
        ttk.Button(actions, text="更新", command=self.refresh).pack(side="left", padx=10)
        ttk.Button(actions, text="再生", command=self.play).pack(side="right")
        ttk.Button(actions, text="範囲クリップ", command=self.preview_clip).pack(side="right", padx=8)
        ttk.Button(actions, text="フォルダを開く", command=self.open_folder).pack(
            side="right", padx=8)
        if analyze:
            ttk.Button(actions, text="文字起こし", command=self.analyze_selected).pack(side="right", padx=8)
        table_frame = ttk.Frame(self.frame)
        table_frame.grid(row=3, column=0, sticky="nsew")
        self.table = ttk.Treeview(table_frame, columns=("title", "range", "size", "date"),
                                  show="headings", selectmode="browse")
        for key, title, width in (("title", "動画タイトル（ダブルクリックで再生）", 450),
                                  ("range", "区間", 110), ("size", "サイズ", 100),
                                  ("date", "保存日時", 155)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=80)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.table.bind("<Double-Button-1>", self.double_click)
        self.table.bind("<Button-3>", self.context_menu)
        self.menu = Menu(self.frame, tearoff=False)
        self.menu.add_command(label="再生", command=self.play)
        self.menu.add_command(label="動画を見ながらクリップ…", command=self.preview_clip)
        self.menu.add_command(label="時刻入力でクリップ…", command=self.clip_dialog)
        if analyze:
            self.menu.add_command(label="文字起こし・範囲解析…", command=self.analyze_selected)
        self.menu.add_separator()
        self.menu.add_command(label="削除（ごみ箱へ）", command=self.delete)
        self.menu.add_command(label="プロパティ", command=self.properties)
        self.table.bind("<Return>", lambda _: self.play())
        ttk.Label(self.frame, textvariable=self.status, wraplength=860).grid(
            row=4, column=0, sticky="w", pady=10)
        ttk.Label(self.frame, text="ダブルクリックするとWindowsの既定の動画プレーヤーで開きます。",
                  wraplength=860).grid(row=5, column=0, sticky="w")
        self.search.trace_add("write", lambda *_: self.render())
        self.directory.trace_add("write", lambda *_: self.invalidate(clear=True))

    @property
    def visible(self):
        return self.notebook.select() == str(self.frame)

    def invalidate(self, clear=False):
        self.revision += 1
        self.dirty = True
        if clear:
            self.records = []
            self.render()
        if self.visible:
            self.start_scan()

    def refresh(self):
        self.invalidate()

    def start_scan(self):
        if self.scanning or not self.dirty:
            return
        self.scanning = True
        self.dirty = False
        directory, revision = self.directory.get(), self.revision
        self.status.set("保存済み動画を読み込んでいます…")

        def scan():
            try:
                rows = list_downloads(directory)
                self.results.put((revision, rows, None))
            except (OSError, ValueError) as error:
                self.results.put((revision, [], str(error)))
        threading.Thread(target=scan, daemon=True).start()

    def poll(self):
        for player in list(self.players):
            for kind, message in player.poll():
                if kind == "error":
                    messagebox.showerror("プレーヤーを開けません", message, parent=self.frame)
            if player.finished:
                player.close()
                self.players.remove(player)
        if self.preview:
            self.preview.poll(self.submit_clip)
        try:
            revision, rows, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.scanning = False
        if revision == self.revision:
            self.records = rows
            self.render()
            if error:
                self.status.set(error)
        if self.dirty and self.visible:
            self.start_scan()

    def render(self):
        selected = self.table.selection()
        old = self.rows.get(selected[0], {}).get("output") if selected else None
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        self.rows = {}
        query = self.search.get().casefold().strip()
        for index, record in enumerate(self.records):
            title = str(record.get("title") or Path(record["output"]).stem)
            if query and query not in (title + " " + record["output"]).casefold():
                continue
            iid = str(index)
            self.rows[iid] = record
            self.table.insert("", "end", iid=iid, values=(
                title, interval_text(record), f"{record['file_size'] / (1024 * 1024):.1f} MB",
                datetime.fromtimestamp(record["modified_at"], tz=UTC).astimezone().strftime(
                    "%Y-%m-%d %H:%M"),
            ))
            if record["output"] == old:
                self.table.selection_set(iid)
        self.status.set(f"{len(self.rows)}件 / {len(self.records)}件 — 行をダブルクリックして再生")

    def select_at(self, event):
        if self.table.identify_region(event.x, event.y) != "cell":
            return False
        iid = self.table.identify_row(event.y)
        if iid not in self.rows:
            return False
        self.table.selection_set(iid)
        self.table.focus(iid)
        return True

    def double_click(self, event):
        if self.select_at(event):
            self.play()

    def context_menu(self, event):
        if self.select_at(event):
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def analyze_selected(self):
        if self.selected_path() is not None and self.analyze:
            self.analyze(dict(self.rows[self.table.selection()[0]]))

    def preview_clip(self):
        path = self.selected_path()
        if path is None:
            return
        try:
            if not path.is_file():
                raise FileNotFoundError("元動画が見つかりません。")
            if self.preview is None:
                self.preview = PreviewServer()
                self.frame.winfo_toplevel().bind("<Destroy>", self.close_preview, add="+")
            record = self.rows[self.table.selection()[0]]
            url = self.preview.add(path, self.directory.get(), record)
            self.players.append(EmbeddedPlayer(url))
            self.status.set("アプリ内プレーヤーで範囲を指定してください。")
        except OSError as error:
            messagebox.showerror("プレビューを開けません", str(error), parent=self.frame)

    def close_preview(self, event):
        if self.preview and event.widget == self.root:
            for player in self.players:
                player.close()
            self.players.clear()
            self.preview.close()
            self.preview = None

    def clip_dialog(self):
        path = self.selected_path()
        if path is None:
            return
        record = dict(self.rows[self.table.selection()[0]])
        directory = self.directory.get()
        window = Toplevel(self.frame)
        window.title("範囲クリップ")
        window.transient(self.frame.winfo_toplevel())
        window.grab_set()
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=path.name, wraplength=550).grid(row=0, column=0, columnspan=2, pady=8)
        ttk.Label(frame, text="選択したファイルの先頭を0秒として指定（時:分:秒・小数可）").grid(
            row=1, column=0, columnspan=2, pady=8)
        start = StringVar(value="00:00:00")
        end = StringVar(value="00:00:30")
        for row, (label, variable) in enumerate((("開始", start), ("終了", end)), 2):
            ttk.Label(frame, text=label).grid(row=row, column=0, pady=6)
            ttk.Entry(frame, textvariable=variable, width=20).grid(row=row, column=1, pady=6)
        ttk.Label(frame, text="元動画を残して再エンコードし、別のMP4を保存します。", wraplength=550).grid(
            row=4, column=0, columnspan=2, pady=8)

        def submit():
            try:
                request = LocalClipRequest(str(path), directory, parse_time(start.get()),
                                           parse_time(end.get()), record.get("title", ""),
                                           record.get("source_url", ""))
                request.validate()
                self.submit_clip(request)
            except (ValueError, OSError) as error:
                messagebox.showerror("指定範囲を確認してください", str(error), parent=window)
                return
            window.destroy()
        ttk.Button(frame, text="切り出し開始", command=submit).grid(row=5, column=1, pady=10)
        return window, start, end, submit

    def delete(self):
        path = self.selected_path()
        if path is None:
            return
        if not messagebox.askyesno("動画を削除", f"次の動画をごみ箱へ移しますか？\n\n{path.name}",
                                   parent=self.frame):
            return
        try:
            recycle_file(path, self.frame.winfo_id())
        except OSError as error:
            messagebox.showerror("削除できません", str(error), parent=self.frame)
        self.refresh()

    def properties(self):
        path = self.selected_path()
        if path is None:
            return
        try:
            stat = path.stat()
        except OSError as error:
            messagebox.showerror("プロパティを表示できません", str(error), parent=self.frame)
            self.refresh()
            return
        record = self.rows[self.table.selection()[0]]
        window = Toplevel(self.frame)
        window.title("動画のプロパティ")
        window.transient(self.frame.winfo_toplevel())
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        values = [("タイトル", record.get("title") or path.stem), ("ファイル", str(path)),
                  ("サイズ", f"{stat.st_size:,} bytes ({stat.st_size / 1048576:.1f} MB)"),
                  ("区間", interval_text(record)),
                  ("保存日時", datetime.fromtimestamp(stat.st_mtime, tz=UTC).astimezone().strftime(
                      "%Y-%m-%d %H:%M:%S")), ("元URL", record.get("source_url", ""))]
        for row, (label, value) in enumerate(values):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(frame, width=70)
            entry.insert(0, str(value))
            entry.configure(state="readonly")
            entry.grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(frame, text="閉じる", command=window.destroy).grid(
            row=len(values), column=1, sticky="e", pady=(12, 0))

    def selected_path(self):
        selection = self.table.selection()
        if not selection or selection[0] not in self.rows:
            self.status.set("再生する動画を選択してください。")
            return None
        return Path(self.rows[selection[0]]["output"])

    def play(self):
        path = self.selected_path()
        if path is None:
            return
        try:
            if not path.is_file() or path.stat().st_size == 0:
                raise FileNotFoundError("動画が見つかりません。移動・削除されている可能性があります。")
            now = time.monotonic()
            if self.last_open[0] == str(path) and now - self.last_open[1] < 0.8:
                return
            os.startfile(str(path))
            self.last_open = (str(path), now)
        except OSError as error:
            messagebox.showerror("再生できません", str(error), parent=self.frame)
            self.refresh()

    def open_folder(self):
        path = self.selected_path()
        if path:
            try:
                if not path.parent.is_dir():
                    raise FileNotFoundError("保存フォルダが見つかりません。")
                os.startfile(str(path.parent))
            except OSError as error:
                messagebox.showerror("フォルダを開けません", str(error), parent=self.frame)
