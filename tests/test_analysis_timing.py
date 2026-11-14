import tempfile
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from clip_downloader.analysis import AnalysisRequest
from clip_downloader.analysis_ui import AnalysisPanel


class AnalysisTimingTests(unittest.TestCase):
    def test_completed_duration_local_datetime_and_unfinished_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            try:
                panel = AnalysisPanel(ttk.Notebook(root), {}, lambda: None)
                video = Path(directory) / 'video.mp4'
                video.touch()
                job = panel.queue.add(AnalysisRequest(str(video)))
                panel.table.insert('', 'end', iid=job.id)
                panel.refresh(job)
                self.assertEqual(panel.table.item(job.id, 'values')[-2:], ('—', '—'))
                job.state = 'done'
                completed = '2026-10-12T12:34:56+00:00'
                job.terminal = {'type': 'done', 'result': {'subtitle_count': 1,
                                'output': 'result.json', 'elapsed_seconds': 3661.25,
                                'completed_at': completed}}
                panel.event(job, {'type': 'finished'})
                expected = datetime.fromisoformat(completed).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                self.assertEqual(panel.table.item(job.id, 'values')[-2:], ('01:01:01', expected))
                # Legacy results can display their creation time, but never invent duration.
                job.terminal['result'] = {'created_at': completed}
                panel.refresh(job)
                self.assertEqual(panel.table.item(job.id, 'values')[-2:], ('—', expected))
                job.state = 'error'
                panel.refresh(job)
                self.assertEqual(panel.table.item(job.id, 'values')[-2:], ('—', '—'))
            finally:
                root.destroy()
