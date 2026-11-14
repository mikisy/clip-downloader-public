import functools
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import imageio_ffmpeg

from clip_downloader.download import DownloadRequest, parse_time, run_download
from clip_downloader.jobs import DownloadQueue


class ValidationTests(unittest.TestCase):
    def test_time_formats(self):
        for text, expected in (("1:02:03.5", 3723.5), ("03:12", 192), ("55.4", 55.4)):
            self.assertEqual(parse_time(text), expected)
        for text in ("-1", "NaN", "1:60", "1::2", ""):
            with self.assertRaises(ValueError):
                parse_time(text)

    def test_invalid_request(self):
        for request in (
            DownloadRequest("file:///secret", str(Path.cwd())),
            DownloadRequest("https://example.com/video", "relative"),
            DownloadRequest("https://example.com/video", str(Path.cwd()), start=5, end=2),
            DownloadRequest("https://example.com/video", str(Path.cwd()), start=0),
        ):
            with self.assertRaises(ValueError):
                request.validate()


class LocalDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([
            cls.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", "-c:v", "libx264",
            "-g", "10", "-c:a", "aac", "-movflags", "+faststart", str(cls.root / "sample.mp4"),
        ], check=True, capture_output=True)

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
            Handler, directory=str(cls.root)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/sample.mp4"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_full_range_metadata_and_no_overwrite(self):
        outputs = []
        for start, end in ((None, None), (1, 3), (None, None)):
            events = []
            request = DownloadRequest(self.url, str(self.root / "保存動画"), start=start, end=end,
                                      skip_existing=False)
            result = run_download(request, events.append)
            output = Path(result["output"])
            self.assertTrue(output.is_file())
            metadata = json.loads((output.parent / "clip.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["requested_range"], [start, end])
            probe = subprocess.run([self.ffmpeg, "-i", str(output), "-f", "null", "-"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   check=False)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertIn("Audio:", probe.stderr)
            self.assertIn("Video:", probe.stderr)
            if start is not None:
                self.assertRegex(probe.stderr, r"Duration: 00:00:0[23]\.")
            self.assertTrue(any(e["type"] == "progress" for e in events))
            outputs.append(output)
        self.assertEqual(len(set(outputs)), 3)
        self.assertTrue(all(path.exists() for path in outputs))

    def test_skip_existing_and_deleted_output(self):
        request = DownloadRequest(self.url, str(self.root / "history"))
        first = run_download(request, lambda _: None)
        # No extractor/network access is permitted on the duplicate path.
        from unittest.mock import patch
        with patch("clip_downloader.download.yt_dlp.YoutubeDL",
                   side_effect=AssertionError("Duplicate started network request")):
            again = run_download(request, lambda _: None)
        self.assertTrue(again["skipped"])
        self.assertEqual(again["output"], first["output"])
        Path(first["output"]).unlink()
        fresh = run_download(request, lambda _: None)
        self.assertNotEqual(fresh["output"], first["output"])
        self.assertTrue(Path(fresh["output"]).exists())

    def test_parallel_duplicate_workers(self):
        downloads = DownloadQueue(limit=2)
        request = DownloadRequest(self.url, str(self.root / "parallel"))
        first = downloads.add(request)
        second = downloads.add(request)
        downloads.launch_pending()
        self.assertEqual(len(downloads.active), 2)
        try:
            deadline = time.monotonic() + 30
            while downloads.busy and time.monotonic() < deadline:
                downloads.poll(lambda *_: None)
                time.sleep(0.05)
            self.assertFalse(downloads.busy)
            self.assertEqual(sorted([first.state, second.state]), ["done", "skipped"])
            self.assertEqual(len(list(Path(request.directory).glob("*/*.mp4"))), 1)
        finally:
            downloads.cancel_all()
            for job in downloads.active:
                job.process.join(timeout=5)

    @unittest.skipUnless(os.environ.get("CLIP_TEST_EXE"), "Packaged executable not requested")
    def test_packaged_gui_worker_download(self):
        output = self.root / "packaged"
        result = subprocess.run([
            os.environ["CLIP_TEST_EXE"], "--smoke-download", self.url + "\n" + self.url + "\n" + self.url + "?other=1",
            "--smoke-directory", str(output),
        ], env={**os.environ, "LOCALAPPDATA": str(self.root / "settings")},
            capture_output=True, timeout=40, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(list(output.glob("*/*.mp4"))), 2)
        self.assertEqual(len(list(output.glob("*/clip.json"))), 2)


if __name__ == "__main__":
    unittest.main()
