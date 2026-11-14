import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import imageio_ffmpeg

from clip_downloader.download import DownloadRequest
from clip_downloader.history import find_existing
from clip_downloader.jobs import DownloadQueue
from clip_downloader.library import list_downloads
from clip_downloader.local_clip import LocalClipRequest, duration_of, run_local_clip


class LocalClipTests(unittest.TestCase):
    def test_export_audio_duration_source_and_invalid_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "日本語 元動画.mp4"
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=160x90:rate=30", "-f", "lavfi", "-i",
                            "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-c:a", "aac",
                            str(source)], check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            original = source.read_bytes()
            request = LocalClipRequest(str(source), directory, 0.5, 1.7, "テスト", "https://youtu.be/WEN9Y8_tav4")
            events = []
            result = run_local_clip(request, events.append)
            output = Path(result["output"])
            self.assertAlmostEqual(duration_of(output), 1.2, delta=0.1)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(any(e["type"] == "clip_progress" for e in events))
            self.assertEqual(len(list_downloads(directory)), 1)
            self.assertIsNone(find_existing(DownloadRequest(request.source_url, directory, start=0.5, end=1.7)))
            self.assertEqual(json.loads((output.parent / "clip.json").read_text(encoding="utf-8"))["kind"], "local_clip")
            second = run_local_clip(request, events.append)
            self.assertNotEqual(second["output"], result["output"])
            queue = DownloadQueue(limit=1)
            job = queue.add(request)
            queue.launch_pending()
            deadline = time.monotonic() + 20
            try:
                while queue.busy and time.monotonic() < deadline:
                    queue.poll(lambda *_: None)
                    time.sleep(0.05)
                self.assertEqual(job.state, "done", job.progress)
                self.assertAlmostEqual(duration_of(job.terminal["result"]["output"]), 1.2, delta=0.1)
            finally:
                queue.cancel_all()
                deadline = time.monotonic() + 10
                while queue.busy and time.monotonic() < deadline:
                    queue.poll(lambda *_: None)
                    time.sleep(0.05)
            with self.assertRaises(ValueError):
                run_local_clip(LocalClipRequest(str(source), directory, 2, 4), events.append)
            with self.assertRaises(ValueError):
                LocalClipRequest(str(source), directory, 2, 1).validate()
            check = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(output),
                                    "-map", "0:a:0", "-f", "null", "-"], capture_output=True, check=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(check.returncode, 0)

    def test_ordered_multi_ranges_with_and_without_audio(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as directory:
            for audio in (True, False):
                source = Path(directory) / f"source-{audio}.mp4"
                command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                           "color=red:size=160x90:rate=30:duration=2", "-f", "lavfi", "-i",
                           "color=blue:size=160x90:rate=30:duration=2"]
                if audio:
                    command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
                command += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"]
                if audio:
                    command += ["-map", "2:a", "-c:a", "aac"]
                subprocess.run(command + ["-c:v", "libx264", str(source)], check=True,
                               creationflags=flags)
                pairs = ((2.5, 3.5), (0.25, 1.25), (2.5, 3.0))
                request = LocalClipRequest(str(source), directory, *pairs[0], ranges=pairs)
                result = run_local_clip(request, lambda _: None)
                self.assertEqual(result["requested_ranges"], [list(pair) for pair in pairs])
                self.assertIsNone(result["requested_range"])
                self.assertAlmostEqual(duration_of(result["output"]), 2.5, delta=0.15)
                for at, channel in [(0.4, 2), (1.4, 0), (2.3, 2)]:
                    frame = subprocess.run([ffmpeg, "-v", "error", "-ss", str(at), "-i",
                                            result["output"], "-frames:v", "1", "-vf", "scale=1:1",
                                            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                           capture_output=True, check=True, creationflags=flags).stdout
                    self.assertEqual(len(frame), 3)
                    self.assertGreater(frame[channel], 200)
                self.assertFalse(list(Path(result["output"]).parent.glob("segments-*")))
                self.assertFalse(list(Path(result["output"]).parent.glob("*.part.mp4")))
                check = subprocess.run([ffmpeg, "-v", "error", "-i", result["output"],
                                        "-map", "0:a:0", "-f", "null", "-"],
                                       capture_output=True, check=False, creationflags=flags)
                self.assertEqual(check.returncode == 0, audio)
                with self.assertRaises(ValueError):
                    run_local_clip(LocalClipRequest(str(source), directory, 0, 1,
                                                    ranges=((0, 1), (3, 5))), lambda _: None)
                with self.assertRaises(ValueError):
                    LocalClipRequest(str(source), directory, 0, 1,
                                     ranges=((float("nan"), 2),)).validate()


if __name__ == "__main__":
    unittest.main()
