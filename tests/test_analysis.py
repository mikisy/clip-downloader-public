import json
import os
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clip_downloader.analysis import (
    AnalysisRequest,
    correct_text,
    parse_corrections,
    run_analysis,
    select_device,
)
from clip_downloader.app import Application


class AnalysisTests(unittest.TestCase):
    def test_corrections_longest_match_and_no_cascade(self):
        rules = parse_corrections('ミオシャ => ミオしゃ\nミオ => 大神ミオ\n不要 => ')
        self.assertEqual(correct_text('ミオシャ、ミオ 不要', rules), 'ミオしゃ、大神ミオ ')
        for invalid in ('=> x', 'a => b\na => c', 'no arrow'):
            with self.assertRaises(ValueError):
                parse_corrections(invalid)

    def test_range_offset_raw_words_unique_outputs_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / '日本語.mp4'
            video.touch()
            playback = Path(directory) / 'aac.mp4'
            playback.write_bytes(b'prepared')
            request = AnalysisRequest(str(video), 60, 70, hotwords=('ミオしゃ',))
            segment = SimpleNamespace(start=1.5, end=3, text='ミオシャ', words=[
                SimpleNamespace(start=1.5, end=3, word='ミオシャ', probability=.9)])
            events = []
            with patch('clip_downloader.analysis.duration_of', return_value=120), \
                 patch('clip_downloader.analysis.prepare_analysis_media', return_value=playback), \
                 patch('clip_downloader.analysis.subprocess.run', return_value=SimpleNamespace(returncode=0)), \
                 patch('clip_downloader.analysis.select_device', return_value='cpu'), \
                 patch('clip_downloader.analysis.read_audio', return_value=None), \
                 patch('faster_whisper.WhisperModel') as model:
                model.return_value.transcribe.side_effect = lambda *a, **kw: (iter([segment]), SimpleNamespace(language='ja'))
                result = run_analysis(request, events.append)
                second = run_analysis(request, events.append)
                self.assertNotEqual(result['output'], second['output'])
                data = json.loads(Path(result['output']).read_text(encoding='utf-8'))
                self.assertEqual(data['source_file'], str(playback))
                self.assertEqual(data['source_size'], playback.stat().st_size)
                self.assertEqual(data['original_source_file'], str(video))
                row = data['segments'][0]
                self.assertEqual((row['start'], row['end']), (61.5, 63))
                self.assertEqual(row['raw_text'], 'ミオシャ')
                self.assertEqual(row['text'], 'ミオしゃ')
                self.assertEqual(row['words'][0]['start'], 61.5)
                self.assertIsNone(row['speaker'])
                self.assertIn('00:01:01,500 --> 00:01:03,000', Path(result['output']).with_name('subtitles.srt').read_text(encoding='utf-8'))
                self.assertEqual(data['analyzed_range'], [60, 70])
                self.assertGreaterEqual(data['elapsed_seconds'], 0)
                self.assertEqual(result['elapsed_seconds'], data['elapsed_seconds'])
                self.assertEqual(result['completed_at'], data['created_at'])
                self.assertTrue(data['started_at'])
                self.assertEqual(model.return_value.transcribe.call_args.kwargs['hotwords'], 'ミオしゃ')
                self.assertFalse(model.return_value.transcribe.call_args.kwargs['vad_filter'])
                self.assertEqual(data['timing_policy'], 'original_audio_clock_v1')
                self.assertFalse(list(Path(directory).rglob('audio.part.wav')))
                with self.assertRaises(ValueError):
                    run_analysis(AnalysisRequest(str(video), 119, 121), events.append)
                model.return_value.transcribe.side_effect = RuntimeError('test failure')
                with self.assertRaises(RuntimeError):
                    run_analysis(request, events.append)
                self.assertEqual(len(list(Path(directory).rglob('analysis.json'))), 2)
                self.assertEqual(len(list(Path(directory).rglob('error.txt'))), 1)

    def test_validation_and_gpu_unavailable(self):
        with patch('ctranslate2.get_cuda_device_count', return_value=0):
            events = []
            self.assertEqual(select_device('auto', events.append), 'cpu')
            self.assertTrue(events)
            with self.assertRaises(RuntimeError):
                select_device('cuda', events.append)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.touch()
            for start, end in ((0, None), (None, 2), (-1, 2), (2, 2), (0, float('nan'))):
                with self.assertRaises(ValueError):
                    AnalysisRequest(str(path), start, end).validate()


class AnalysisGuiTests(unittest.TestCase):
    def test_library_selection_range_settings_duplicate_pending_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'settings.json'
            with patch('clip_downloader.app.settings_file', return_value=settings):
                root = tk.Tk()
                app = Application(root)
                try:
                    panel = app.analysis
                    video = Path(directory) / '日本語.mp4'
                    video.touch()
                    app.library.rows['fixture'] = {'output': str(video), 'source_url': 'https://youtu.be/example'}
                    app.library.table.insert('', 'end', iid='fixture')
                    app.library.table.selection_set('fixture')
                    app.library.analyze_selected()
                    self.assertEqual(panel.path.get(), str(video))
                    self.assertEqual(app.notebook.select(), str(panel.frame))
                    panel.partial.set(True)
                    panel.update_range()
                    panel.start.set('00:01:02.5')
                    panel.end.set('00:01:12.5')
                    with patch.object(panel.queue, 'launch_pending'):
                        panel.submit()
                        panel.submit()
                    self.assertEqual(len(panel.queue.jobs), 1)
                    request = panel.queue.jobs[0].request
                    self.assertEqual((request.start, request.end), (62.5, 72.5))
                    self.assertEqual(json.loads(settings.read_text(encoding='utf-8'))['analysis']['model'], 'large-v3')
                    self.assertEqual(panel.queue.limit, 1)
                    self.assertFalse(app.downloads.busy)
                    panel.table.selection_set('1')
                    panel.cancel_selected()
                    self.assertFalse(panel.queue.busy)
                    panel.start.set('bad')
                    with patch('clip_downloader.analysis_ui.messagebox.showerror') as error:
                        panel.submit()
                        error.assert_called_once()
                    self.assertEqual(len(panel.queue.jobs), 1)
                finally:
                    # Cancel Tk polling before destroying this test root.
                    for task in root.tk.call('after', 'info'):
                        root.after_cancel(task)
                    root.destroy()


def delayed_worker(request, events):
    events.put({'type': 'analysis_progress', 'text': '解析中'})
    time.sleep(120)


class AnalysisQueueTests(unittest.TestCase):
    def test_real_process_cancellation_serializes_analysis(self):
        from clip_downloader.jobs import DownloadQueue
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'video.mp4'
            video.touch()
            queue = DownloadQueue(limit=1, worker_target=delayed_worker)
            first = queue.add(AnalysisRequest(str(video)))
            second = queue.add(AnalysisRequest(str(video), 0, 1))
            queue.launch_pending()
            self.assertEqual(second.state, 'pending')
            queue.cancel_all()
            deadline = time.monotonic() + 10
            while queue.busy and time.monotonic() < deadline:
                queue.poll(lambda *_: None)
                time.sleep(.05)
            self.assertFalse(queue.busy)
            self.assertEqual(first.state, 'cancelled')
            self.assertEqual(second.state, 'cancelled')


@unittest.skipUnless(os.environ.get('CLIP_ANALYSIS_SAMPLE'), 'Set CLIP_ANALYSIS_SAMPLE for real inference')
class AnalysisRealTests(unittest.TestCase):
    def test_real_cpu_sample(self):
        events = []
        result = run_analysis(AnalysisRequest(os.environ['CLIP_ANALYSIS_SAMPLE'], 1, 9,
                                             model='tiny', device='cpu'), events.append)
        data = json.loads(Path(result['output']).read_text(encoding='utf-8'))
        self.assertTrue(data['segments'])
        self.assertTrue(all(1 <= row['start'] < row['end'] <= 9 for row in data['segments']))
