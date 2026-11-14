import time
import tkinter as tk
import unittest
from unittest.mock import patch

from clip_downloader.search import SearchResult
from clip_downloader.sources import SourceDialog, fetch_sources, source_candidates


class SourceTests(unittest.TestCase):
    def test_links_context_dedup_and_exclusions(self):
        description = '''元配信はこちら
Original stream title
https://youtu.be/abcdefghijk?t=123
https://www.youtube.com/live/abcdefghijk?si=test
別の元動画 https://www.youtube.com/watch?v=12345678901&amp;t=30
https://www.youtube.com/shorts/12345678902
https://www.youtube.com/@Channel
https://www.youtube.com/playlist?list=12345678901
https://www.youtube.com/watch?v=ownvideo001
https://evil.example/watch?v=12345678903
https://youtube.com.evil.example/watch?v=12345678904'''
        rows = source_candidates(description, 'ownvideo001')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['url'], 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertIn('元配信はこちら', rows[0]['context'])
        self.assertEqual(source_candidates('リンクなし', 'ownvideo001'), [])

    def test_metadata_only(self):
        with patch('clip_downloader.sources.yt_dlp.YoutubeDL') as factory:
            factory.return_value.__enter__.return_value.extract_info.return_value = {
                'description': '元動画 https://youtu.be/abcdefghijk'}
            description, rows = fetch_sources('https://youtu.be/ownvideo001')
            self.assertIn('元動画', description)
            self.assertEqual(len(rows), 1)
            factory.return_value.__enter__.return_value.extract_info.assert_called_once_with(
                'https://www.youtube.com/watch?v=ownvideo001', download=False)
            self.assertTrue(factory.call_args.args[0]['skip_download'])


class SourceGuiTests(unittest.TestCase):
    def test_show_copy_add_retry_and_close(self):
        root = tk.Tk()
        row = SearchResult('ownvideo001', 'Clip', 'Channel', 10)
        received = []
        candidates = [{'url': 'https://www.youtube.com/watch?v=abcdefghijk',
                       'context': '元配信 https://youtu.be/abcdefghijk'}]
        try:
            with patch('clip_downloader.sources.fetch_sources', return_value=('元配信全文', candidates)):
                dialog = SourceDialog(root, row, received.extend)
                self.wait(root, dialog)
            self.assertIn('元配信全文', dialog.description.get('1.0', 'end'))
            dialog.table.selection_set('0')
            dialog.selected()
            dialog.copy()
            self.assertEqual(root.clipboard_get(), candidates[0]['url'])
            dialog.add()
            self.assertEqual(received, [candidates[0]['url']])
            self.assertTrue(dialog.closed)
            with patch('clip_downloader.sources.fetch_sources', side_effect=OSError('offline')):
                dialog = SourceDialog(root, row, received.extend)
                self.wait(root, dialog)
            self.assertIn('offline', dialog.status.get())
            with patch('clip_downloader.sources.fetch_sources', return_value=('', [])):
                dialog.load()
                self.wait(root, dialog)
            self.assertIn('特定できません', dialog.status.get())
            self.assertTrue(dialog.add_button.instate(['disabled']))
            dialog.window.destroy()
        finally:
            root.destroy()

    def wait(self, root, dialog):
        deadline = time.monotonic() + 5
        while dialog.loading and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        self.assertFalse(dialog.loading)


if __name__ == '__main__':
    unittest.main()
