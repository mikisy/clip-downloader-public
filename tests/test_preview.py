import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from clip_downloader.preview import PreviewServer


class PreviewTests(unittest.TestCase):
    def test_range_auth_and_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '日本語.mp4'
            path.write_bytes(b'0123456789')
            server = PreviewServer()
            try:
                url = server.add(path, directory, {'title': '<script>not html</script>'})
                with urlopen(url, timeout=5) as response:
                    page = response.read().decode()
                    self.assertIn('&lt;script&gt;', page)
                for value, expected in [('bytes=2-5', b'2345'), ('bytes=7-', b'789'), ('bytes=-3', b'789')]:
                    with urlopen(Request(url+'media', headers={'Range': value}), timeout=5) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.read(), expected)
                for target, headers in [(server.origin+'/wrong/media', {}), (url+'media', {'Range': 'bytes=99-'})]:
                    with self.assertRaises(HTTPError):
                        urlopen(Request(target, headers=headers), timeout=5)
                payload = json.dumps({'start': 1.2, 'end': 3.4}).encode()
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(url+'clip', data=payload, headers={
                        'Content-Type': 'application/json', 'Origin': 'https://example.com'}), timeout=5)
                self.assertEqual(denied.exception.code, 403)
                with urlopen(Request(url+'clip', data=payload, headers={
                        'Content-Type': 'application/json', 'Origin': server.origin}), timeout=5) as response:
                    key = json.load(response)['id']
                requests = []
                server.poll(requests.append)
                self.assertEqual(len(requests), 1)
                self.assertEqual((requests[0].start, requests[0].end), (1.2, 3.4))
                self.assertEqual(requests[0].url, str(path))
                def post_ranges(pairs):
                    return urlopen(Request(url+'clip', data=json.dumps({'ranges': pairs}).encode(),
                                           headers={'Content-Type': 'application/json'}), timeout=5)
                with post_ranges([[4, 5], [1, 2], [4, 4.5]]):
                    pass
                server.poll(requests.append)
                self.assertEqual(requests[-1].intervals, ((4, 5), (1, 2), (4, 4.5)))
                for invalid in ([], [[2, 1]], [[0]], [[0, float('nan')]], [[0, 1]]*101, 'bad'):
                    with self.assertRaises(HTTPError) as rejected:
                        post_ranges(invalid)
                    self.assertEqual(rejected.exception.code, 400)
                with urlopen(url+'result/'+key, timeout=5) as response:
                    self.assertTrue(json.load(response)['done'])
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
