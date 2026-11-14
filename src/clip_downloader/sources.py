"""Read description links as source candidates, never as verified provenance."""

import html
import queue
import re
import threading
import webbrowser
from tkinter import StringVar, Text, Toplevel, messagebox, ttk
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from clip_downloader.download import deno_path
from clip_downloader.video_menu import VideoContextMenu


def video_id_from_url(url):
    try:
        parsed = urlsplit(html.unescape(url))
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or '').lower()
        parts = parsed.path.strip('/').split('/')
        if host in ('youtu.be', 'www.youtu.be'):
            value = parts[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if parsed.path == '/watch':
                value = parse_qs(parsed.query).get('v', [''])[0]
            elif len(parts) == 2 and parts[0] in ('live', 'shorts', 'embed'):
                value = parts[1]
            else:
                return None
        else:
            return None
        return value if re.fullmatch(r'[A-Za-z0-9_-]{11}', value) else None
    except ValueError:
        return None


def source_candidates(description, own_id):
    rows = {}
    lines = description.splitlines()
    for i, line in enumerate(lines):
        for match in re.finditer(r'https?://[^\s<>"\u3000]+', line):
            video_id = video_id_from_url(match.group().rstrip('.,;!?)】」』）'))
            if not video_id or video_id == own_id or video_id in rows:
                continue
            context = ' / '.join(part.strip() for part in lines[max(0, i - 2):i + 2]
                                 if part.strip())
            rows[video_id] = {'url': f'https://www.youtube.com/watch?v={video_id}',
                              'context': context[:500]}
    return list(rows.values())


def fetch_sources(video_url):
    own_id = video_id_from_url(video_url)
    if not own_id:
        raise ValueError('YouTubeの単体動画URLを指定してください。')
    options = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
               'cachedir': False, 'socket_timeout': 12, 'retries': 1, 'extractor_retries': 1,
               'ignore_no_formats_error': True, 'js_runtimes': {'deno': {'path': deno_path()}}}
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(f'https://www.youtube.com/watch?v={own_id}', download=False)
    description = (info or {}).get('description') or ''
    return description, source_candidates(description, own_id)


class SourceDialog:
    def __init__(self, parent, video, add_urls, queue_urls=None):
        self.add_urls = add_urls
        self.queue_urls = queue_urls
        self.source_dialog = None
        self.video = video
        self.window = Toplevel(parent)
        self.window.title('説明欄から元動画を確認')
        self.window.geometry('880x630')
        self.window.minsize(650, 480)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(2, weight=1)
        self.window.rowconfigure(5, weight=1)
        self.closed = False
        self.loading = False
        self.pending = queue.SimpleQueue()
        self.rows = []
        self.url = StringVar()
        self.status = StringVar()
        ttk.Label(self.window, text=video.title, wraplength=820).grid(
            row=0, column=0, sticky='w', padx=12, pady=10)
        controls = ttk.Frame(self.window)
        controls.grid(row=1, column=0, sticky='ew', padx=12)
        ttk.Label(controls, text='説明欄の動画リンク候補（元動画かどうかは記載を確認）').pack(side='left')
        self.retry = ttk.Button(controls, text='再取得', command=self.load)
        self.retry.pack(side='right')
        candidate_frame = ttk.Frame(self.window)
        candidate_frame.grid(row=2, column=0, sticky='nsew', padx=12, pady=8)
        self.table = ttk.Treeview(candidate_frame, columns=('url', 'context'),
                                  show='headings', selectmode='browse', height=5)
        self.table.heading('url', text='動画URL')
        self.table.column('url', width=310, minwidth=280)
        self.table.heading('context', text='説明欄の前後の記載')
        self.table.column('context', width=470, minwidth=200)
        scrollbar = ttk.Scrollbar(candidate_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.context_menu = VideoContextMenu(
            self.table, self.queue_selected, self.add,
            self.show_sources, self.open_selected, queue_urls is not None)
        self.table.bind('<<TreeviewSelect>>', self.selected)
        actions = ttk.Frame(self.window)
        actions.grid(row=3, column=0, sticky='ew', padx=12)
        actions.columnconfigure(0, weight=1)
        ttk.Entry(actions, textvariable=self.url, state='readonly').grid(
            row=0, column=0, sticky='ew', padx=(0, 8))
        self.copy_button = ttk.Button(actions, text='URLコピー', command=self.copy, state='disabled')
        self.copy_button.grid(row=0, column=1)
        self.add_button = ttk.Button(actions, text='DL欄へ追加', command=self.add, state='disabled')
        self.add_button.grid(row=0, column=2, padx=8)
        ttk.Label(self.window, textvariable=self.status, wraplength=820).grid(
            row=4, column=0, sticky='w', padx=12, pady=8)
        description_frame = ttk.LabelFrame(self.window, text='説明欄全文（選択してコピーできます）')
        description_frame.grid(row=5, column=0, sticky='nsew', padx=12, pady=(0, 12))
        self.description = Text(description_frame, height=9, wrap='word', state='disabled')
        scroll = ttk.Scrollbar(description_frame, command=self.description.yview)
        self.description.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.description.pack(fill='both', expand=True)
        self.timer = self.window.after(100, self.poll)
        self.window.bind('<Destroy>', self.destroyed)
        self.load()

    def destroyed(self, event):
        if event.widget == self.window:
            self.closed = True
            self.window.after_cancel(self.timer)

    def load(self):
        if self.loading or self.closed:
            return
        self.loading = True
        self.retry.configure(state='disabled')
        self.status.set('選択した動画の説明欄を取得中…（動画本体はDLしません）')
        self.rows = []
        self.table.delete(*self.table.get_children())
        self.selected()
        self.description.configure(state='normal')
        self.description.delete('1.0', 'end')
        self.description.configure(state='disabled')

        def run():
            try:
                description, rows = fetch_sources(self.video.url)
                result = (description, rows, None)
            except (yt_dlp.utils.DownloadError, OSError, ValueError, RuntimeError) as error:
                result = ('', [], str(error))
            if not self.closed:
                self.pending.put(result)
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            description, rows, error = self.pending.get_nowait()
        except queue.Empty:
            pass
        else:
            self.loading = False
            self.retry.configure(state='normal')
            self.rows = rows
            self.description.configure(state='normal')
            self.description.insert('1.0', description)
            self.description.configure(state='disabled')
            for i, row in enumerate(rows):
                self.table.insert('', 'end', iid=str(i), values=(row['url'], row['context']))
            if error:
                self.status.set(f'取得できませんでした。再取得できます：{error[:250]}')
            elif rows:
                self.status.set(f'{len(rows)}件の候補。関連動画・宣伝も含まれるため、説明欄で出典を確認してください。')
            else:
                self.status.set('説明欄に別のYouTube動画URLは見つかりませんでした。元動画は特定できません。')
        self.timer = self.window.after(100, self.poll)

    def selected(self, _event=None):
        selection = self.table.selection()
        self.url.set(self.rows[int(selection[0])]['url'] if selection else '')
        state = 'normal' if self.url.get() else 'disabled'
        self.copy_button.configure(state=state)
        self.add_button.configure(state=state)

    def copy(self):
        if self.url.get():
            self.window.clipboard_clear()
            self.window.clipboard_append(self.url.get())
            self.status.set('選択した動画URLをコピーしました。')

    def add(self):
        if self.url.get():
            self.add_urls([self.url.get()])
            self.status.set('ダウンロード欄へ追加しました。設定を確認してDLを開始してください。')
            self.window.destroy()

    def queue_selected(self):
        self.selected()
        if self.url.get() and self.queue_urls and self.queue_urls([self.url.get()]):
            self.status.set('DLキューに追加しました。')

    def open_selected(self):
        self.selected()
        if not self.url.get():
            return
        try:
            if not webbrowser.open(self.url.get()):
                raise OSError('既定のブラウザを起動できませんでした。')
        except OSError as error:
            messagebox.showerror('動画を開けません', str(error), parent=self.window)

    def show_sources(self):
        self.selected()
        if not self.url.get():
            return
        if self.source_dialog and not self.source_dialog.closed:
            if self.source_dialog.video.url == self.url.get():
                self.source_dialog.window.lift()
                return
            self.source_dialog.window.destroy()
        video = SimpleNamespace(url=self.url.get(), title=self.url.get(),
                                id=video_id_from_url(self.url.get()))
        self.source_dialog = SourceDialog(self.window, video, self.add_urls, self.queue_urls)
