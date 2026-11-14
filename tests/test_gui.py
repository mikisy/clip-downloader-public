import multiprocessing as mp
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_downloader.app import Application
from clip_downloader.download import DownloadRequest
from clip_downloader.jobs import DownloadQueue


def sleeping_worker(request, events):
    time.sleep(120)


class QueueTests(unittest.TestCase):
    def test_bounded_parallelism_pending_cancel_and_release(self):
        with tempfile.TemporaryDirectory() as folder:
            downloads = DownloadQueue(limit=2, worker_target=sleeping_worker)
            for i in range(4):
                downloads.add(DownloadRequest(f"https://example.org/{i}", folder))
            downloads.launch_pending()
            self.assertEqual(len(downloads.active), 2)
            downloads.cancel(downloads.jobs[2])
            downloads.cancel(downloads.jobs[0])
            try:
                end = time.monotonic() + 10
                while downloads.jobs[3].state == "pending" and time.monotonic() < end:
                    downloads.poll(lambda *_: None)
                    self.assertLessEqual(len(downloads.active), 2)
                    time.sleep(0.05)
                self.assertEqual(downloads.jobs[3].state, "running")
                self.assertEqual(downloads.jobs[1].state, "running")
                self.assertEqual(downloads.jobs[0].state, "cancelled")
                self.assertEqual(downloads.jobs[2].state, "cancelled")
            finally:
                downloads.cancel_all()
                end = time.monotonic() + 10
                while downloads.busy and time.monotonic() < end:
                    downloads.poll(lambda *_: None)
                    time.sleep(0.05)
                self.assertFalse(downloads.busy)


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = Application(self.root)
        self.temp = tempfile.TemporaryDirectory()
        self.settings_patch = patch("clip_downloader.app.settings_file",
                                    return_value=Path(self.temp.name) / "settings.json")
        self.settings_patch.start()
        self.app.directory.set(self.temp.name)

    def tearDown(self):
        self.app.downloads.cancel_all()
        for job in self.app.downloads.active:
            if job.stop_thread:
                job.stop_thread.join(timeout=5)
            job.process.join(timeout=5)
        self.root.destroy()
        self.settings_patch.stop()
        self.temp.cleanup()

    def wait_until(self, predicate, timeout=40):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if predicate():
                return
            time.sleep(0.03)
        self.fail("GUI operation timed out")

    def test_range_controls_and_invalid_batch_is_atomic(self):
        self.assertTrue(self.app.start_entry.instate(["disabled"]))
        self.app.partial.set(True)
        self.app.update_range()
        self.assertFalse(self.app.start_entry.instate(["disabled"]))
        self.app.url_text.insert("1.0", "https://example.org/video\ninvalid")
        with patch("tkinter.messagebox.showerror") as error:
            self.app.start()
            error.assert_called_once()
        self.assertEqual(self.app.downloads.jobs, [])

    def test_multiline_queue_selected_cancel_and_add_while_running(self):
        self.app.downloads.worker_target = sleeping_worker
        self.app.parallel.set("2")
        self.app.url_text.insert("1.0", "https://example.org/one\nhttps://example.org/two")
        self.app.start()
        self.assertEqual(len(self.app.downloads.active), 2)
        self.app.url_text.insert("1.0", "https://example.org/three")
        self.app.start()
        self.assertEqual(self.app.downloads.jobs[2].state, "pending")
        self.app.table.selection_set("1")
        self.app.cancel_selected()
        self.wait_until(lambda: self.app.downloads.jobs[2].state == "running")
        self.assertEqual(self.app.downloads.jobs[1].state, "running")
        self.app.cancel()
        self.wait_until(lambda: not self.app.downloads.busy)
        self.assertTrue(all(j.state == "cancelled" for j in self.app.downloads.jobs))

    def test_worker_error_restores_queue_and_saves_settings(self):
        self.app.url_text.insert("1.0", "http://127.0.0.1:1/missing.mp4")
        self.app.start()
        self.wait_until(lambda: not self.app.downloads.busy)
        self.assertEqual(self.app.downloads.jobs[0].state, "error")
        self.assertTrue((Path(self.temp.name) / "settings.json").exists())
        self.assertFalse(self.app.start_button.instate(["disabled"]))


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main()
