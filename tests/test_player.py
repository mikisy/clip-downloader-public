import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import imageio_ffmpeg

from clip_downloader.player import smoke_player


@unittest.skipUnless(os.name == 'nt', 'Windows WebView2 integration')
class PlayerTests(unittest.TestCase):
    def test_native_playback_seek_and_range(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '再生テスト.mp4'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi',
                            '-i', 'testsrc2=size=320x180:rate=30', '-f', 'lavfi', '-i',
                            'sine=frequency=440', '-t', '3', '-c:v', 'libx264', '-c:a', 'aac',
                            '-movflags', '+faststart', str(path)], check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            if os.environ.get('CLIP_TEST_EXE'):
                result = subprocess.run([os.environ['CLIP_TEST_EXE'], '--smoke-player', str(path)],
                                        timeout=50, check=False)
                self.assertEqual(result.returncode, 0)
            else:
                self.assertTrue(smoke_player(path))


if __name__ == '__main__':
    unittest.main()
