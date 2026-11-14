import json
import multiprocessing as mp
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from clip_downloader.download import DownloadRequest
from clip_downloader.history import download_lock, find_existing, source_key


def hold_lock(request, acquired):
    with download_lock(request, lambda _: None):
        acquired.set()
        time.sleep(120)


def probe_lock(request, acquired):
    with download_lock(request, lambda _: None):
        acquired.set()


class HistoryTests(unittest.TestCase):
    def test_process_termination_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            request = DownloadRequest("https://youtu.be/WEN9Y8_tav4", folder)
            ctx = mp.get_context("spawn")
            acquired = ctx.Event()
            first = ctx.Process(target=hold_lock, args=(request, acquired))
            first.start()
            try:
                self.assertTrue(acquired.wait(10))
            finally:
                first.terminate()
                first.join(timeout=5)
            acquired.clear()
            second = ctx.Process(target=probe_lock, args=(request, acquired))
            second.start()
            try:
                self.assertTrue(acquired.wait(10))
                second.join(timeout=5)
                self.assertEqual(second.exitcode, 0)
            finally:
                if second.is_alive():
                    second.terminate()
                    second.join(timeout=5)

    def test_url_aliases(self):
        urls = ["https://www.youtube.com/watch?v=WEN9Y8_tav4&t=12s",
                "https://youtu.be/WEN9Y8_tav4?si=anything",
                "https://www.youtube.com/live/WEN9Y8_tav4",
                "https://m.youtube.com/watch?v=WEN9Y8_tav4"]
        self.assertEqual(len({source_key(url) for url in urls}), 1)
        self.assertNotEqual(source_key(urls[0]), source_key("https://youtu.be/aaaaaaaaaaa"))

    def test_metadata_ranges_missing_and_moved_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "video.mp4"
            output.write_bytes(b"complete")
            request = DownloadRequest("https://youtu.be/WEN9Y8_tav4", folder)
            data = {"output": str(root / "old" / "video.mp4"),
                    "source_url": "https://www.youtube.com/watch?v=WEN9Y8_tav4",
                    "requested_range": [None, None], "output_size": 8}
            (root / "clip.json").write_text(json.dumps(data))
            self.assertEqual(find_existing(request)["output"], str(output))
            self.assertIsNotNone(find_existing(replace(request, quality="720p")))
            self.assertIsNone(find_existing(replace(request, start=0, end=10)))
            output.write_bytes(b"truncated")
            self.assertIsNone(find_existing(request))
            output.unlink()
            self.assertIsNone(find_existing(request))
            (root / "clip.json").write_text("invalid")
            self.assertIsNone(find_existing(request))


if __name__ == "__main__":
    unittest.main()
