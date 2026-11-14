import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from clip_downloader.app import Application
from clip_downloader.search import SearchResult, search_videos


class SearchTests(unittest.TestCase):
    def test_metadata_only_and_normalization(self):
        with patch('clip_downloader.search.yt_dlp.YoutubeDL') as factory:
            engine = factory.return_value.__enter__.return_value
            engine.extract_info.return_value = {'entries': [
                {'id': 'abcdefghijk', 'title': '日本語の動画', 'channel': 'Channel',
                 'duration': 3661}, None,
                {'id': 'abcdefghijk', 'title': 'Duplicate'},
                {'id': 'UC-not-a-video', 'title': 'Channel'},
                {'id': '12345678901', 'uploader': 'Live', 'duration': None,
                 'live_status': 'is_live'},
                {'id': '12345678902', 'duration': float('nan')},
            ]}
            rows = search_videos('  みこ タンポポ  ', 10)
            engine.extract_info.assert_called_once_with('ytsearch10:みこ タンポポ', download=False)
            self.assertTrue(factory.call_args.args[0]['skip_download'])
            self.assertTrue(factory.call_args.args[0]['extract_flat'])
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0].url, 'https://www.youtube.com/watch?v=abcdefghijk')
            self.assertEqual(rows[0].duration_text, '01:01:01')
            self.assertEqual(rows[1].duration_text, 'ライブ中')
            self.assertEqual(rows[2].duration_text, '不明')

    def test_invalid_search_and_empty_results(self):
        with patch('clip_downloader.search.yt_dlp.YoutubeDL') as factory:
            for query, limit in [('', 10), ('a' * 201, 20), ('test', 100)]:
                with self.assertRaises(ValueError):
                    search_videos(query, limit)
            factory.assert_not_called()
            factory.return_value.__enter__.return_value.extract_info.return_value = None
            self.assertEqual(search_videos('no matches'), [])


class SearchGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = Application(self.root)
        self.search = self.app.search
        self.root.update()

    def tearDown(self):
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.root.update()
            self.search.poll()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Search UI timed out')

    def test_async_results_thumbnails_transfer_and_retry(self):
        release = threading.Event()
        rows = [SearchResult('abcdefghijk', 'Miko', 'hololive', 180),
                SearchResult('12345678901', 'Second', 'Channel', None)]

        def blocked_search(*_):
            release.wait(3)
            return rows

        with patch('clip_downloader.search.search_videos', side_effect=blocked_search), \
             patch('clip_downloader.search.thumbnail_image',
                   return_value=Image.new('RGB', (120, 68))):
            self.search.query.set('Miko')
            self.search.start()
            self.root.update()
            self.assertTrue(self.search.busy)
            self.app.notebook.select(self.app.download_frame)
            self.assertEqual(self.app.notebook.select(), str(self.app.download_frame))
            release.set()
            self.wait_for(lambda: len(self.search.images) == 2)
            self.assertFalse(self.search.busy)
            self.app.url_text.insert('1.0', 'https://example.com/keep\n' + rows[0].url)
            self.search.table.selection_set([row.id for row in rows])
            self.search.add_selected()
            self.assertEqual(self.app.notebook.tabs()[0], str(self.search.frame))
            self.assertEqual(self.app.notebook.select(), str(self.app.download_frame))
            value = self.app.url_text.get('1.0', 'end')
            self.assertIn('https://example.com/keep', value)
            self.assertEqual(value.count(rows[0].url), 1)
            self.assertIn(rows[1].url, value)
            self.assertEqual(self.app.downloads.jobs, [])
            self.assertEqual(self.app.notebook.select(), str(self.app.download_frame))
            with patch('clip_downloader.search.webbrowser.open', return_value=True) as browser:
                self.search.table.selection_set(rows[0].id)
                self.search.open_selected()
                browser.assert_called_once_with(rows[0].url)

        with patch('clip_downloader.search.search_videos', side_effect=RuntimeError('offline')):
            self.search.start()
            self.wait_for(lambda: not self.search.busy)
            self.assertIn('offline', self.search.status.get())
            self.assertFalse(self.search.search_button.instate(['disabled']))
        with patch('clip_downloader.search.search_videos', return_value=[]):
            old_revision = self.search.revision
            self.search.start()
            self.search.results.put((old_revision, 'rows', rows))
            self.wait_for(lambda: not self.search.busy)
            self.assertEqual(self.search.rows, {})
            self.assertIn('見つかりません', self.search.status.get())

    def test_missing_thumbnail_and_destroy_during_search(self):
        row = SearchResult('abcdefghijk', 'Still selectable', 'Channel', 20)
        with patch('clip_downloader.search.search_videos', return_value=[row]), \
             patch('clip_downloader.search.thumbnail_image', side_effect=OSError('offline')):
            self.search.query.set('test')
            self.search.start()
            self.wait_for(lambda: not self.search.busy)
            self.assertIn(row.id, self.search.table.get_children())
            self.assertEqual(self.search.images, {})
        self.search.frame.destroy()
        self.assertTrue(self.search.closed)
        self.assertTrue(self.search.cancelled.is_set())
        self.search.poll()


if __name__ == '__main__':
    unittest.main()
