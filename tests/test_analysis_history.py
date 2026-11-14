import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

from clip_downloader.analysis_history import (
    AnalysisHistory,
    display_values,
    read_record,
    scan_history,
    wsl_path,
)


def fixture(root, name='result'):
    source = root / '日本語 sample.mp4'
    source.touch()
    path = root / 'analysis' / name / 'analysis.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'kind': 'whisper_analysis', 'schema_version': 1,
        'id': 'a'*32, 'source_file': str(source), 'analyzed_range': [60, 70],
        'created_at': '2026-10-12T12:30:00+07:00', 'segments': []}), encoding='utf-8')
    return path


class HistoryTests(unittest.TestCase):
    def test_old_new_incomplete_corrupt_dedup_and_path_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = fixture(root)
            bad = root / 'bad' / 'analysis.json'
            bad.parent.mkdir()
            bad.write_text('{}')
            (root / 'analysis.tmp').write_text('{}')
            bad_list = root / 'invalid-list' / 'analysis.json'
            bad_list.parent.mkdir()
            bad_list.write_text('[]')
            rows, skipped = scan_history(str(root), [str(path)])
            self.assertEqual(len(rows), 1)
            self.assertEqual(skipped, 2)
            self.assertEqual(display_values(rows[0])[3], '不明')
            rows[0]['elapsed_seconds'] = 3661
            self.assertEqual(display_values(rows[0])[3], '01:01:01')
            self.assertEqual(wsl_path('D:\\Youtube\\日本語 clip\\analysis.json'), '/mnt/d/Youtube/日本語 clip/analysis.json')
            with self.assertRaises(ValueError):
                wsl_path('\\\\server\\share\\analysis.json')

    def test_portable_package_survives_move_and_prefers_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis = fixture(root)
            package = analysis.parent / 'studio' / ('b' * 32)
            (package / 'media').mkdir(parents=True)
            (package / 'media/source.mp4').write_bytes(b'video')
            (package / 'job.json').write_text('{}')
            manifest = package / 'studio-package.json'
            manifest.write_text(json.dumps({'kind': 'studio_package', 'version': 1,
                'job_id': 'b'*32, 'source_file': 'media/source.mp4', 'title': '編集済み',
                'count': 3, 'analyzed_range': [0, 10]}), encoding='utf-8')
            rows, skipped = scan_history(root)
            self.assertEqual((len(rows), skipped), (1, 0))
            moved = root / 'moved'
            package.rename(moved)
            row = read_record(moved / 'studio-package.json')
            self.assertTrue(Path(row['source_file']).is_file())
            self.assertEqual(display_values(row)[0], '編集済み')

    def test_async_reload_selection_open_and_error_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture(Path(directory))
            root = tk.Tk()
            try:
                book = ttk.Notebook(root)
                book.pack(fill='both', expand=True)
                history = AnalysisHistory(book, tk.StringVar(value=directory))
                book.select(history.frame)
                history.reload()
                self.wait(root, history, lambda: not history.scanning)
                self.assertEqual(len(history.rows), 1)
                history.table.selection_set('0')
                expected = 'http://localhost:4174/?job=' + 'a'*32 + '&view=browse'
                with patch('clip_downloader.analysis_history.open_in_studio', return_value=expected) as importer, \
                     patch('clip_downloader.analysis_history.webbrowser.open', return_value=True) as browser:
                    history.open_button.invoke()
                    history.open_selected()  # Double activation must not create a second import.
                    self.wait(root, history, lambda: not history.opening)
                    importer.assert_called_once()
                    browser.assert_called_once_with(expected)
                with patch('clip_downloader.analysis_history.open_in_studio', side_effect=RuntimeError('WSL unavailable')):
                    history.open_selected()
                    self.wait(root, history, lambda: not history.opening)
                    self.assertIn('WSL unavailable', history.status.get())
                    self.assertFalse(history.open_button.instate(['disabled']))
                history.reload()
                self.wait(root, history, lambda: not history.scanning)
                self.assertTrue(history.selected())
            finally:
                root.destroy()

    def test_codex_review_button_progress_report_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture(Path(directory))
            root = tk.Tk()
            root.geometry('780x650')
            try:
                book = ttk.Notebook(root)
                book.pack(fill='both', expand=True)
                history = AnalysisHistory(book, tk.StringVar(value=directory))
                book.select(history.frame)
                history.reload()
                self.wait(root, history, lambda: not history.scanning)
                history.table.selection_set('0')
                root.update()
                self.assertLessEqual(history.report_button.winfo_rootx() + history.report_button.winfo_width(),
                                     root.winfo_rootx() + root.winfo_width())
                def review(path, studio, progress):
                    progress('日本語チェック中')
                    return {'report': str(Path(directory) / 'report.txt'), 'fixed': 2, 'checks': 1}
                with patch('clip_downloader.analysis_history.review_in_codex', side_effect=review) as runner:
                    history.review_button.invoke()
                    history.review_selected()
                    self.assertTrue(history.open_button.instate(['disabled']))
                    self.wait(root, history, lambda: not history.reviewing)
                    runner.assert_called_once()
                    self.assertIn('修正 2件', history.status.get())
                    with patch('clip_downloader.analysis_history.os.startfile', create=True) as opener:
                        history.report_button.invoke()
                        opener.assert_called_once_with(str(Path(directory) / 'report.txt'))
                with patch('clip_downloader.analysis_history.review_in_codex', side_effect=RuntimeError('login required')):
                    history.review_button.invoke()
                    self.wait(root, history, lambda: not history.reviewing)
                    self.assertIn('login required', history.status.get())
                    self.assertFalse(history.review_button.instate(['disabled']))
            finally:
                root.destroy()

    def test_review_status_survives_reload_and_ignores_failed_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fixture(Path(directory))
            def report(name, applied, items):
                folder = path.parent / 'codex-reviews' / name
                folder.mkdir(parents=True)
                (folder / 'report.json').write_text(json.dumps({'applied': applied, 'items': items}))
                (folder / 'report.txt').write_text('review')
                return str(folder / 'report.txt')
            self.assertEqual(scan_history(directory)[0][0]['review_status'], '未チェック')
            saved = report('20261111-001', True, [{'status': 'check'}, {'status': 'fix'}])
            report('20261111-002', False, [])
            rows, _ = scan_history(directory)
            self.assertEqual(rows[0]['review_status'], 'チェック済み・要確認1件')
            self.assertEqual(rows[0]['review_report'], saved)
            report('20261111-003', True, [])
            self.assertEqual(scan_history(directory)[0][0]['review_status'], 'チェック済み')

    def test_context_menu_selects_clicked_row_and_restores_its_report(self):
        with tempfile.TemporaryDirectory() as directory:
            first = fixture(Path(directory), 'first')
            fixture(Path(directory), 'second')
            folder = first.parent / 'codex-reviews' / '20261111-001'
            folder.mkdir(parents=True)
            (folder / 'report.json').write_text(json.dumps({'applied': True, 'items': []}))
            report = folder / 'report.txt'
            report.write_text('checked')
            root = tk.Tk()
            root.geometry('1000x650')
            try:
                book = ttk.Notebook(root)
                book.pack(fill='both', expand=True)
                history = AnalysisHistory(book, tk.StringVar(value=directory))
                book.select(history.frame)
                history.reload()
                self.wait(root, history, lambda: not history.scanning)
                checked = next(i for i, row in history.rows.items() if row['path'] == str(first.resolve()))
                other = next(i for i in history.rows if i != checked)
                history.table.selection_set(other)
                root.update()
                x, y, _, height = history.table.bbox(checked)
                event = SimpleNamespace(x=x+10, y=y+height//2, x_root=50, y_root=50)
                with patch.object(history.menu, 'tk_popup') as popup:
                    history.context_menu(event)
                    popup.assert_called_once_with(50, 50)
                self.assertEqual(history.selected()['path'], str(first.resolve()))
                self.assertEqual(history.review_report, str(report))
                with patch('clip_downloader.analysis_history.os.startfile', create=True) as opener:
                    history.menu.invoke(2)
                    opener.assert_called_once_with(str(report))
                with patch('clip_downloader.analysis_history.open_in_studio', return_value='http://localhost:4174/') as opener, \
                        patch('clip_downloader.analysis_history.webbrowser.open'):
                    history.menu.invoke(1)
                    self.wait(root, history, lambda: not history.opening)
                    self.assertEqual(opener.call_args.args[0], str(first.resolve()))
                history.reviewing = True
                with patch.object(history.menu, 'tk_popup'):
                    history.context_menu(event)
                self.assertEqual(history.menu.entrycget(0, 'state'), 'disabled')
                self.assertEqual(history.menu.entrycget(1, 'state'), 'disabled')
                history.reviewing = False
                history.table.selection_set(other)
                root.update()
                self.assertIsNone(history.review_report)
                self.assertTrue(history.report_button.instate(['disabled']))
                history.reload()
                self.wait(root, history, lambda: not history.scanning)
                self.assertEqual(history.rows[checked]['review_status'], 'チェック済み')
            finally:
                root.destroy()

    def wait(self, root, history, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root.update()
            history.poll()
            if predicate():
                return
            time.sleep(.01)
        self.fail('operation timed out')
