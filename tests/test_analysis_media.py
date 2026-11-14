import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg

from clip_downloader.analysis_media import prepare_analysis_media


class AnalysisMediaTests(unittest.TestCase):
    def test_real_conversion_cache_and_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / '日本語.mp4'
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=10',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                            '-c:v', 'libx264', '-c:a', 'libopus', str(source)], check=True)
            before = source.read_bytes()
            output = prepare_analysis_media(source, lambda _: None)
            self.assertEqual(source.read_bytes(), before)
            self.assertNotEqual(source, output)
            info = subprocess.run([ffmpeg, '-hide_banner', '-i', str(output)],
                                  capture_output=True, text=True, errors='replace', check=False).stderr
            self.assertIn('Audio: aac', info)
            # Encoded video payload remains identical (stream copy).
            def video_bytes(path):
                return subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(path),
                                                '-map', '0:v:0', '-c', 'copy', '-f', 'h264', '-'])
            self.assertEqual(video_bytes(source), video_bytes(output))
            with patch('clip_downloader.analysis_media.subprocess.run', side_effect=AssertionError('cache miss')):
                self.assertEqual(prepare_analysis_media(source, lambda _: None), output)
            output.write_bytes(b'broken')
            repaired = prepare_analysis_media(source, lambda _: None)
            self.assertGreater(repaired.stat().st_size, 6)
            source.touch()
            self.assertNotEqual(prepare_analysis_media(source, lambda _: None), output)

    def test_failed_conversion_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bad.mp4'
            source.write_bytes(b'bad')
            with self.assertRaises(RuntimeError):
                prepare_analysis_media(source, lambda _: None)
            self.assertFalse(list(Path(directory).rglob('aac-source.json')))
            self.assertFalse(list(Path(directory).rglob('*.part.mp4')))
