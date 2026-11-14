import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clip_downloader.app import Application
from clip_downloader.library import list_downloads


def save_video(root, name="動画テスト", title="日本語の動画タイトル"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{name}.mp4"
    video.write_bytes(b"fixture-data")
    record = {"title": title, "output": str(video), "output_size": video.stat().st_size,
              "requested_range": [None, None], "source_url": "https://youtu.be/WEN9Y8_tav4"}
    (folder / "clip.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return video


class LibraryDataTests(unittest.TestCase):
    def test_completed_missing_corrupt_empty_and_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = save_video(root)
            save_video(root, "missing").unlink()
            save_video(root, "empty").write_bytes(b"")
            corrupt = save_video(root, "corrupt")
            (corrupt.parent / "clip.json").write_text("invalid")
            executable = root / "program.exe"
            executable.write_bytes(b"anything")
            (root / "clip.json").write_text(json.dumps({"output": str(executable)}))
            videos = list_downloads(directory)
            self.assertEqual([row["output"] for row in videos], [str(expected)])


class LibraryGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.app = Application(self.root)
        self.app.directory.set(self.temp.name)
        self.library = self.app.library

    def tearDown(self):
        # Finish the background read before removing its fixture directory.
        end = time.monotonic() + 5
        while self.library.scanning and time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)
        self.root.destroy()
        self.temp.cleanup()

    def wait(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            self.root.update()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Library did not update")

    def show(self):
        self.app.notebook.select(self.library.frame)
        self.wait(lambda: not self.library.scanning and not self.library.dirty)

    def test_tab_click_search_and_deleted_file(self):
        video = save_video(Path(self.temp.name))
        self.show()
        self.assertEqual(len(self.library.rows), 1)
        iid = next(iter(self.library.rows))
        self.root.update()
        x, y, _, height = self.library.table.bbox(iid)
        event = SimpleNamespace(x=x + 15, y=y + height // 2)
        with patch("clip_downloader.library.os.startfile", create=True) as player:
            self.library.table.event_generate("<ButtonPress-1>", x=event.x, y=event.y)
            self.library.table.event_generate("<ButtonRelease-1>", x=event.x, y=event.y)
            player.assert_not_called()
            self.library.double_click(event)
            player.assert_called_once_with(str(video))
            self.library.play()  # Double-click does not launch another player.
            player.assert_called_once()
        self.library.search.set("見つからない")
        self.assertEqual(len(self.library.rows), 0)
        self.library.search.set("日本語")
        self.assertEqual(len(self.library.rows), 1)
        self.library.table.selection_set(next(iter(self.library.rows)))
        video.unlink()
        with patch("clip_downloader.library.messagebox.showerror") as error:
            self.library.play()
            error.assert_called_once()
        self.wait(lambda: not self.library.scanning and not self.library.rows)

    def test_context_delete_confirmation_and_properties(self):
        video = save_video(Path(self.temp.name))
        self.show()
        iid = next(iter(self.library.rows))
        x, y, _, height = self.library.table.bbox(iid)
        event = SimpleNamespace(x=x + 15, y=y + height // 2, x_root=50, y_root=50)
        with patch.object(self.library.menu, "tk_popup") as popup:
            self.library.context_menu(event)
            popup.assert_called_once_with(50, 50)
        self.assertEqual(self.library.selected_path(), video)
        self.library.properties()
        windows = [w for w in self.library.frame.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(windows), 1)
        windows[0].destroy()
        with patch("clip_downloader.library.messagebox.askyesno", return_value=False), \
                patch("clip_downloader.library.recycle_file") as recycle:
            self.library.delete()
            recycle.assert_not_called()
        with patch("clip_downloader.library.messagebox.askyesno", return_value=True), \
                patch("clip_downloader.library.recycle_file", side_effect=lambda p, _: p.unlink()) as recycle:
            self.library.delete()
            recycle.assert_called_once()
        self.wait(lambda: not self.library.scanning and not self.library.rows)
        self.assertTrue((video.parent / "clip.json").is_file())

    def test_visual_preview_uses_selected_video_and_drains_queue(self):
        video = save_video(Path(self.temp.name))
        self.show()
        self.library.table.selection_set(next(iter(self.library.rows)))
        with patch("clip_downloader.library.EmbeddedPlayer") as browser:
            self.library.preview_clip()
            self.assertIn("127.0.0.1", browser.call_args.args[0])
            browser.return_value.poll.return_value = []
            browser.return_value.finished = False
            record = next(iter(self.library.preview.sources.values()))
            self.assertEqual(record["path"], str(video))
        self.library.preview.pending.put(("test", "request"))
        with patch.object(self.library, "submit_clip") as submit:
            self.library.poll()
            submit.assert_called_once_with("request")
        self.assertTrue(self.library.preview.results["test"]["done"])

    def test_clip_dialog_validates_and_submits_selected_file(self):
        video = save_video(Path(self.temp.name))
        self.show()
        self.library.table.selection_set(next(iter(self.library.rows)))
        window, start, end, submit = self.library.clip_dialog()
        with patch.object(self.library, "submit_clip") as callback, \
                patch("clip_downloader.library.messagebox.showerror") as error:
            start.set("00:00:05.5")
            end.set("00:00:02")
            submit()
            error.assert_called_once()
            callback.assert_not_called()
            end.set("00:00:08")
            submit()
            request = callback.call_args.args[0]
            self.assertEqual(request.url, str(video))
            self.assertEqual((request.start, request.end), (5.5, 8))
            self.assertFalse(window.winfo_exists())

    def test_directory_switch_discards_old_scan_and_keeps_download_input(self):
        save_video(Path(self.temp.name))
        self.app.url_text.insert("1.0", "https://example.org/video")
        self.show()
        other = Path(self.temp.name) / "other"
        other.mkdir()
        release, entered = threading.Event(), threading.Event()

        def slow_scan(directory):
            if directory == self.temp.name:
                entered.set()
                release.wait(5)
            return list_downloads(directory)

        with patch("clip_downloader.library.list_downloads", side_effect=slow_scan):
            self.library.refresh()
            self.assertTrue(entered.wait(3))
            self.app.directory.set(str(other))
            release.set()
            self.wait(lambda: not self.library.scanning and not self.library.dirty)
        self.assertEqual(self.library.records, [])
        self.assertIn("https://example.org/video", self.app.url_text.get("1.0", "end"))

    def test_new_completion_refreshes_visible_tab(self):
        self.show()
        video = save_video(Path(self.temp.name))
        self.library.invalidate()
        self.wait(lambda: not self.library.scanning and len(self.library.rows) == 1)
        self.assertEqual(next(iter(self.library.rows.values()))["output"], str(video))


if __name__ == "__main__":
    unittest.main()
