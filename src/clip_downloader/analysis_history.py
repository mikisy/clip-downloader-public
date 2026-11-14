"""Reload completed local analyses and import them into the existing WSL Studio."""
import json
import math
import os
import queue
import re
import subprocess
import threading
import webbrowser
from datetime import datetime
from pathlib import Path, PurePosixPath
from tkinter import Menu, StringVar, filedialog, ttk

STUDIO = ''


def wsl_path(path):
    value = str(path)
    if not re.match(r'^[A-Za-z]:[\\/]', value):
        raise ValueError('Studio連携はWindowsのローカルドライブ上の解析に対応しています。')
    return '/mnt/' + value[0].lower() + '/' + value[3:].replace('\\', '/')


def review_summary(path):
    """Historical completion, not a claim that later edits have been reviewed."""
    roots = [path.parent / 'codex-reviews']
    # A newly imported package still lives beside its original recognition file.
    if path.name == 'studio-package.json' and path.parent.parent.name == 'studio':
        original = path.parent.parent.parent
        if (original / 'analysis.json').is_file():
            roots.append(original / 'codex-reviews')
    candidates = sorted((p for root in roots for p in root.glob('*/report.json')),
                        key=lambda p: p.parent.name, reverse=True)
    for report in candidates:
        try:
            if report.stat().st_size > 10 * 1024 * 1024:
                continue
            data = json.loads(report.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('applied') is not True:
                continue
            items = data.get('items')
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                continue
            checks = sum(item.get('status') == 'check' for item in items)
            text = report.with_suffix('.txt')
            return {'review_status': f'チェック済み・要確認{checks}件' if checks else 'チェック済み',
                    'review_report': str(text if text.is_file() else report)}
        except (OSError, ValueError, TypeError):
            continue
    return {'review_status': '未チェック', 'review_report': None}


def read_record(path):
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('解析JSONが大きすぎます。')
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, dict) and data.get('kind') == 'studio_package':
        if data.get('version') != 1 or not re.fullmatch(r'[a-f0-9]{32}', data.get('job_id', '')):
            raise ValueError('未対応のStudioデータです。')
        source = Path(data['source_file'])
        if source.is_absolute() or '..' in source.parts:
            raise ValueError('Studioデータの素材パスが不正です。')
        return {'path': str(path.resolve()), 'source_file': str(path.parent / source),
                'title': data.get('title', source.name), 'package': True,
                'count': data['count'], 'range': data['analyzed_range'],
                'created_at': data.get('created_at'), 'elapsed_seconds': data.get('elapsed_seconds')}
    if (not isinstance(data, dict) or data.get('kind') != 'whisper_analysis' or data.get('schema_version') != 1
            or not isinstance(data.get('segments'), list)
            or not re.fullmatch(r'[a-f0-9]{32}', data.get('id', ''))):
        raise ValueError('Clip Downloaderの完成した解析JSONを選んでください。')
    start, end = data['analyzed_range']
    if not isinstance(data['source_file'], str) or not data['source_file'] or not all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
        raise ValueError('動画・解析範囲の記録が不正です。')
    created = data.get('completed_at') or data.get('created_at')
    if not isinstance(created, str):
        created = None
    return {'path': str(path.resolve()), 'source_file': data['source_file'],
            'count': len(data['segments']), 'range': data['analyzed_range'],
            'created_at': created,
            'elapsed_seconds': data.get('elapsed_seconds')}


def scan_history(directory, extra_paths=()):
    root = Path(directory)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('ダウンロード画面で有効な保存先フォルダを指定してください。')
    paths = list(root.rglob('analysis.json')) + list(root.rglob('studio-package.json')) + [Path(p) for p in extra_paths]
    rows, skipped, seen = [], 0, set()
    for path in paths:
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            row = read_record(path)
            # Prefer a portable package over its original recognition JSON.
            if not row.get('package'):
                packages = list((path.parent / 'studio').glob('*/studio-package.json'))
                if any((package.parent / 'job.json').is_file() for package in packages):
                    continue
            row.update(review_summary(path))
            rows.append(row)
        except (OSError, ValueError, KeyError, TypeError):
            skipped += 1
    return sorted(rows, key=lambda r: r.get('created_at') or '', reverse=True), skipped


def bridge_command(path, studio, script):
    studio_path = PurePosixPath(studio)
    if not studio_path.is_absolute() or ".." in studio_path.parts:
        raise ValueError("WSLのStudioパスは絶対パスで指定してください。")
    engine_python = str(studio_path.parent / "vtuber-clip-engine/.venv/bin/python")
    return ['wsl.exe', '--exec', engine_python, str(studio_path / 'scripts' / script),
            '--analysis', wsl_path(path)]


def review_in_codex(path, studio, progress):
    command = bridge_command(path, studio, 'review-downloader-analysis.py')
    result = None
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace',
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'progress':
                progress(event['text'])
            elif event.get('type') in ('done', 'error'):
                result = event
        code = process.wait()
    if not result or result.get('type') != 'done' or code:
        raise RuntimeError((result or {}).get('text') or 'Codex連携を実行できません。WSLとStudioの配置を確認してください。')
    report = result.get('report', '')
    match = re.fullmatch(r'/mnt/([a-z])/(.+)', report)
    if not match:
        raise ValueError('修正レポートの保存先がWindowsドライブではありません。')
    result['report'] = match[1].upper() + ':\\' + match[2].replace('/', '\\')
    return result


def open_in_studio(path, studio=STUDIO):
    command = bridge_command(path, studio, 'open-downloader-analysis.py')
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=1800, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('取り込みの応答がタイムアウトしました。再度開くと同じ解析を確認して再利用します。') from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1800:] or 'Studio連携を開始できません。WSLとStudioの配置を確認してください。')
    data = json.loads(result.stdout)
    if not re.fullmatch(r'http://localhost:4174/\?job=[a-f0-9]{32}&view=browse', data.get('url', '')):
        raise ValueError('Studioからの応答が不正です。')
    return data['url']


def display_values(row):
    elapsed = row.get('elapsed_seconds')
    if isinstance(elapsed, (int, float)) and 0 <= elapsed < 1e12:
        seconds = round(elapsed)
        elapsed = f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'
    else:
        elapsed = '不明'
    try:
        created = datetime.fromisoformat(row['created_at']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        created = '不明'
    start, end = row['range']
    return (row.get('title') or Path(row['source_file']).name, f'{start:g}〜{end:g}', row['count'], elapsed,
            created, 'Studioで開く', row.get('review_status', '未チェック'))


class AnalysisHistory:
    def __init__(self, notebook, directory, studio=STUDIO, save_settings=None):
        self.notebook, self.directory = notebook, directory
        self.studio_location = StringVar(value=studio)
        self.frame = ttk.Frame(notebook, padding=18)
        notebook.add(self.frame, text='解析履歴')
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)
        self.rows = {}
        self.extra_paths = set()
        self.events = queue.SimpleQueue()
        self.scanning = False
        self.opening = False
        self.revision = 0
        self.dirty = True
        self.status = StringVar(value='保存済みの解析結果を再読み込みできます。')
        ttk.Label(self.frame, textvariable=directory).grid(row=0, column=0, sticky='w')
        toolbar = ttk.Frame(self.frame)
        toolbar.grid(row=1, column=0, sticky='ew', pady=10)
        actions = ttk.Frame(toolbar)
        actions.pack(fill='x')
        review_actions = ttk.Frame(toolbar)
        review_actions.pack(fill='x', pady=(8, 0))
        ttk.Button(actions, text='再読み込み', command=self.reload).pack(side='left')
        ttk.Button(actions, text='解析・Studioデータを開く', command=self.choose).pack(side='left', padx=8)
        self.open_button = ttk.Button(actions, text='Studioで開く', command=self.open_selected)
        self.open_button.pack(side='left')
        self.reviewing = False
        self.review_report = None
        self.review_button = ttk.Button(review_actions, text='Codexで日本語チェック・修正', command=self.review_selected)
        self.review_button.pack(side='left', padx=8)
        self.report_button = ttk.Button(review_actions, text='修正結果', command=self.open_review_report, state='disabled')
        self.report_button.pack(side='left')
        ttk.Button(actions, text='結果フォルダ', command=self.open_folder).pack(side='right')
        holder = ttk.Frame(self.frame)
        holder.grid(row=2, column=0, sticky='nsew')
        self.table = ttk.Treeview(holder, columns=('title', 'range', 'count', 'elapsed', 'date', 'open', 'review'),
                                  displaycolumns=('title', 'review', 'range', 'count', 'elapsed', 'date', 'open'),
                                  show='headings', selectmode='browse')
        for key, name, width in (('title', '動画', 270), ('range', '範囲（秒）', 110),
                                 ('count', '字幕数', 65), ('elapsed', '解析時間', 85),
                                 ('date', '完了日時', 170), ('open', '操作', 100), ('review', 'Codexチェック', 170)):
            self.table.heading(key, text=name)
            self.table.column(key, width=width, minwidth=70)
        horizontal = ttk.Scrollbar(holder, orient='horizontal', command=self.table.xview)
        vertical = ttk.Scrollbar(holder, orient='vertical', command=self.table.yview)
        self.table.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        horizontal.pack(side='bottom', fill='x')
        vertical.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<ButtonRelease-1>', self.click)
        self.table.bind('<Return>', lambda _: self.open_selected())
        self.table.bind('<<TreeviewSelect>>', lambda _: self.sync_review_report())
        self.menu = Menu(self.table, tearoff=False)
        for label, command in (('Codexで日本語チェック・修正', self.review_selected),
                               ('Studioで開く', self.open_selected),
                               ('修正結果を開く', self.open_review_report),
                               ('結果フォルダを開く', self.open_folder)):
            self.menu.add_command(label=label, command=command)
        self.table.bind('<Button-3>', self.context_menu)
        ttk.Label(self.frame, textvariable=self.status, wraplength=860).grid(row=3, column=0, sticky='w', pady=8)
        connection = ttk.Frame(self.frame)
        connection.grid(row=4, column=0, sticky='ew')
        ttk.Label(connection, text='WSLのStudioパス').pack(side='left')
        ttk.Entry(connection, textvariable=self.studio_location, width=64).pack(side='left', padx=8, fill='x', expand=True)
        if save_settings:
            ttk.Button(connection, text='接続先を保存', command=save_settings).pack(side='left')
        self.directory.trace_add('write', lambda *_: self.invalidate())

    @property
    def visible(self):
        return self.notebook.select() == str(self.frame)

    def invalidate(self):
        self.revision += 1
        self.dirty = True

    def reload(self):
        self.invalidate()
        self.scan()

    def scan(self):
        if self.scanning:
            return
        self.dirty = False
        self.scanning = True
        revision, directory, extra = self.revision, self.directory.get(), tuple(self.extra_paths)
        self.status.set('解析履歴を読み込んでいます…')
        def work():
            try:
                rows, skipped = scan_history(directory, extra)
                self.events.put(('scan', revision, rows, skipped, None))
            except (OSError, ValueError) as error:
                self.events.put(('scan', revision, [], 0, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def choose(self):
        path = filedialog.askopenfilename(parent=self.frame, filetypes=[('解析・Studioデータ', '*.json')])
        if path:
            self.extra_paths.add(path)
            self.reload()

    def click(self, event):
        if self.table.identify_region(event.x, event.y) == 'cell' and self.table.column(self.table.identify_column(event.x), 'id') == 'open':
            iid = self.table.identify_row(event.y)
            if iid in self.rows:
                self.table.selection_set(iid)
                self.open_selected()

    def sync_review_report(self):
        self.review_report = (self.selected() or {}).get('review_report')
        self.report_button.configure(state='normal' if self.review_report else 'disabled')

    def context_menu(self, event):
        if self.table.identify_region(event.x, event.y) not in ('tree', 'cell'):
            return
        iid = self.table.identify_row(event.y)
        if iid not in self.rows:
            return
        self.table.selection_set(iid)
        self.table.focus(iid)
        self.table.focus_set()
        self.sync_review_report()
        for index in (0, 1):
            self.menu.entryconfigure(index, state='disabled' if self.reviewing or self.opening else 'normal')
        self.menu.entryconfigure(2, state='normal' if self.review_report else 'disabled')
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return 'break'

    def selected(self):
        selection = self.table.selection()
        return self.rows.get(selection[0]) if selection else None

    def open_selected(self):
        row = self.selected()
        if not row or self.opening or self.reviewing:
            return
        if not Path(row['source_file']).is_file():
            self.status.set('元動画が見つかりません。動画を元の場所に戻してから開いてください。')
            return
        self.opening = True
        self.open_button.configure(state='disabled')
        self.review_button.configure(state='disabled')
        self.status.set('Studioデータを準備中…（初回は動画・素材のコピーと音声抽出を行います）')
        studio = self.studio_location.get().strip()
        def work():
            try:
                self.events.put(('open', open_in_studio(row['path'], studio), None))
            except (OSError, ValueError, RuntimeError) as error:
                self.events.put(('open', None, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def review_selected(self):
        row = self.selected()
        if not row or self.opening or self.reviewing:
            return
        self.reviewing = True
        self.open_button.configure(state='disabled')
        self.review_button.configure(state='disabled')
        self.status.set('Codexで日本語チェックを開始します。処理中は対象字幕の編集を控えてください。')
        studio = self.studio_location.get().strip()

        def work():
            try:
                result = review_in_codex(row['path'], studio,
                                        lambda text: self.events.put(('review_progress', text)))
                self.events.put(('review_done', row['path'], result, None))
            except (OSError, ValueError, RuntimeError) as error:
                self.events.put(('review_done', row['path'], None, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def open_review_report(self):
        if self.review_report:
            try:
                os.startfile(self.review_report)
            except OSError as error:
                self.status.set(str(error))

    def open_folder(self):
        row = self.selected()
        if row:
            try:
                os.startfile(str(Path(row['path']).parent))
            except OSError as error:
                self.status.set(str(error))

    def poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'review_progress':
                self.status.set(event[1])
            elif event[0] == 'review_done':
                _, reviewed_path, result, error = event
                self.reviewing = False
                self.open_button.configure(state='normal')
                self.review_button.configure(state='normal')
                if error:
                    self.status.set(error)
                else:
                    for iid, row in self.rows.items():
                        if row['path'] == reviewed_path:
                            row.update(review_report=result['report'], review_status=(
                                f"チェック済み・要確認{result['checks']}件" if result['checks'] else 'チェック済み'))
                            self.table.item(iid, values=display_values(row))
                    # Discard any scan started before this review completed.
                    self.revision += 1
                    self.sync_review_report()
                    self.status.set(f"日本語チェック完了：修正 {result['fixed']}件・要確認 {result['checks']}件。"
                                    '「修正結果」で確認できます。Studioを開いている場合は再読み込みしてください。')
            elif event[0] == 'scan':
                _, revision, rows, skipped, error = event
                self.scanning = False
                if revision != self.revision:
                    continue
                selected = self.selected()
                old = selected['path'] if selected else None
                self.table.delete(*self.table.get_children())
                self.rows = {str(i): row for i, row in enumerate(rows)}
                for iid, row in self.rows.items():
                    self.table.insert('', 'end', iid=iid, values=display_values(row))
                    if row['path'] == old:
                        self.table.selection_set(iid)
                self.sync_review_report()
                if not self.reviewing:
                    self.status.set(error or f'{len(rows)}件（読めない結果 {skipped}件）— 行の「Studioで開く」をクリック')
            else:
                _, url, error = event
                self.opening = False
                self.open_button.configure(state='normal')
                self.review_button.configure(state='normal')
                if error:
                    self.status.set(error)
                else:
                    opened = webbrowser.open(url)
                    self.status.set('Studioで解析を開きました。' if opened else f'ブラウザで開いてください: {url}')
        if self.visible and self.dirty and not self.scanning:
            self.scan()
