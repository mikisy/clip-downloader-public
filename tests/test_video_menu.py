import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clip_downloader.app import Application
from clip_downloader.search import SearchResult
from clip_downloader.sources import SourceDialog


class VideoMenuTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = Application(self.root)
        self.search = self.app.search
        for key in ('abcdefghijk', '12345678901'):
            row = SearchResult(key, key, 'Channel', 20)
            self.search.rows[key] = row
            self.search.table.insert('', 'end', iid=key, values=(key, 'Channel', '00:20'))

    def tearDown(self):
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def popup(self, context, row):
        event = SimpleNamespace(x=10, y=20, x_root=10, y_root=20)
        with patch.object(context.table, 'identify_region', return_value='cell'), \
             patch.object(context.table, 'identify_row', return_value=row), \
             patch.object(context.menu, 'tk_popup'):
            context.popup(event)

    def test_search_queue_only_selected_preserves_input_and_settings(self):
        self.app.url_text.insert('1.0', 'https://example.com/keep')
        self.app.partial.set(True)
        self.app.start_time.set('00:10')
        self.app.end_time.set('00:20')
        self.popup(self.search.context_menu, '12345678901')
        with patch.object(self.app.downloads, 'launch_pending'), \
             patch.object(self.app, 'save_settings'):
            self.search.context_menu.menu.invoke(0)
        self.assertEqual(len(self.app.downloads.jobs), 1)
        request = self.app.downloads.jobs[0].request
        self.assertEqual(request.url, self.search.rows['12345678901'].url)
        self.assertEqual((request.start, request.end), (10, 20))
        self.assertEqual(self.app.url_text.get('1.0', 'end').strip(), 'https://example.com/keep')

    def test_multi_select_add_and_single_actions(self):
        self.search.table.selection_set(('abcdefghijk', '12345678901'))
        self.popup(self.search.context_menu, 'abcdefghijk')
        self.assertEqual(len(self.search.table.selection()), 2)
        self.assertEqual(self.search.context_menu.menu.entrycget(2, 'state'), 'disabled')
        self.search.context_menu.menu.invoke(1)
        self.assertEqual(len(self.app.url_text.get('1.0', 'end').strip().splitlines()), 2)
        self.assertEqual(self.app.downloads.jobs, [])
        self.search.table.selection_set('abcdefghijk')
        self.popup(self.search.context_menu, '12345678901')
        with patch('clip_downloader.search.webbrowser.open', return_value=True) as opened:
            self.search.context_menu.menu.invoke(3)
            opened.assert_called_once_with(self.search.rows['12345678901'].url)
        with patch('clip_downloader.search.SourceDialog') as dialog:
            self.search.context_menu.menu.invoke(2)
            self.assertEqual(dialog.call_args.args[1].id, '12345678901')
        with patch.object(self.search.table, 'identify_region', return_value='heading'), \
             patch.object(self.search.context_menu.menu, 'tk_popup') as popup:
            self.search.context_menu.popup(SimpleNamespace(x=1, y=1))
            popup.assert_not_called()

    def test_source_menu_queue_open_nested_sources_and_add(self):
        urls, queued = [], []
        def enqueue(values):
            queued.extend(values)
            return True
        with patch.object(SourceDialog, 'load'):
            dialog = SourceDialog(self.root, self.search.rows['abcdefghijk'], urls.extend, enqueue)
            url = 'https://www.youtube.com/watch?v=12345678901'
            dialog.rows = [{'url': url, 'context': 'source'}]
            dialog.table.insert('', 'end', iid='0', values=(url, 'source'))
            self.popup(dialog.context_menu, '0')
            dialog.context_menu.menu.invoke(0)
            self.assertEqual(queued, [url])
            with patch('clip_downloader.sources.webbrowser.open', return_value=True) as opened:
                dialog.context_menu.menu.invoke(3)
                opened.assert_called_once_with(url)
            dialog.context_menu.menu.invoke(2)
            self.assertEqual(dialog.source_dialog.video.url, url)
            dialog.context_menu.menu.invoke(1)
            self.assertEqual(urls, [url])
            self.assertTrue(dialog.closed)
            self.assertTrue(dialog.source_dialog.closed)


if __name__ == '__main__':
    unittest.main()
