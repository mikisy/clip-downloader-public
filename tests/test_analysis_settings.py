import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_downloader.analysis import AnalysisRequest
from clip_downloader.app import Application


class AnalysisSettingsTests(unittest.TestCase):
    def test_save_without_video_or_queue_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'settings.json'
            with patch('clip_downloader.app.settings_file', return_value=target):
                root = tk.Tk()
                app = Application(root)
                panel = app.analysis
                try:
                    panel.hotwords.insert('1.0', 'さくらみこ\nミオしゃ\nさくらみこ')
                    panel.model.set('small')
                    panel.device.set('cpu')
                    panel.partial.set(True)
                    panel.start.set('invalid unused time')
                    panel.save_button.invoke()
                    saved = json.loads(target.read_text(encoding='utf-8'))['analysis']
                    self.assertEqual(saved['hotwords'], ['さくらみこ', 'ミオしゃ'])
                    self.assertEqual(saved['corrections'], {'ミオシャ': 'ミオしゃ'})
                    self.assertEqual(panel.queue.jobs, [])
                    self.assertEqual(app.downloads.jobs, [])
                    self.assertIn('設定を保存しました', panel.status.get())
                finally:
                    for task in root.tk.call('after', 'info'):
                        root.after_cancel(task)
                    root.destroy()
                root = tk.Tk()
                app = Application(root)
                try:
                    self.assertEqual(app.analysis.model.get(), 'small')
                    self.assertEqual(app.analysis.device.get(), 'cpu')
                    self.assertEqual(app.analysis.hotwords.get('1.0', 'end').strip(), 'さくらみこ\nミオしゃ')
                finally:
                    for task in root.tk.call('after', 'info'):
                        root.after_cancel(task)
                    root.destroy()

    def test_save_preserves_existing_job_and_rejects_invalid_or_failed_write(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'settings.json'
            with patch('clip_downloader.app.settings_file', return_value=target):
                root = tk.Tk()
                app = Application(root)
                panel = app.analysis
                try:
                    video = Path(directory) / 'sample.mp4'
                    video.touch()
                    request = AnalysisRequest(str(video), hotwords=('old',))
                    job = panel.queue.add(request)
                    panel.hotwords.insert('1.0', 'new')
                    with patch.object(panel.queue, 'launch_pending') as launch, \
                         patch.object(panel.queue, 'cancel_all') as cancel:
                        panel.save_button.invoke()
                        launch.assert_not_called()
                        cancel.assert_not_called()
                    self.assertEqual(job.request.hotwords, ('old',))
                    self.assertEqual(job.state, 'pending')
                    original = target.read_bytes()
                    previous = panel.saved
                    panel.corrections.insert('end', '\ninvalid')
                    with patch('clip_downloader.analysis_ui.messagebox.showerror') as error:
                        panel.save_button.invoke()
                        error.assert_called_once()
                    self.assertEqual(target.read_bytes(), original)
                    panel.corrections.delete('1.0', 'end')
                    with patch('clip_downloader.app.Path.replace', side_effect=OSError('denied')):
                        panel.save_button.invoke()
                    self.assertEqual(target.read_bytes(), original)
                    self.assertIs(panel.saved, previous)
                    self.assertIn('保存できませんでした', panel.status.get())
                finally:
                    for task in root.tk.call('after', 'info'):
                        root.after_cancel(task)
                    root.destroy()
