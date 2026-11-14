"""Dedicated native WebView2 window, isolated from Tk's UI event loop."""
import multiprocessing as mp
import queue
import threading
import time
from pathlib import Path


def player_worker(url, events, closing, smoke=False):
    try:
        import webview
        window = webview.create_window("Clip Downloader — 範囲クリップ", url, width=1120, height=900,
                                       min_size=(760, 600))
        loaded = threading.Event()
        window.events.loaded += loaded.set
        window.events.closed += closing.set

        def monitor():
            try:
                deadline = time.monotonic() + 25
                while not closing.wait(0.1):
                    if not loaded.is_set():
                        if time.monotonic() > deadline:
                            raise RuntimeError("動画プレビューの起動がタイムアウトしました。")
                        continue
                    if smoke:
                        ready = window.evaluate_js("document.getElementById('video').readyState >= 2")
                        if not ready:
                            if time.monotonic() > deadline:
                                raise RuntimeError("動画を読み込めませんでした。")
                            continue
                        window.evaluate_js("document.getElementById('video').currentTime=0.5")
                        time.sleep(0.3)
                        value = window.evaluate_js("document.getElementById('markStart').click(); document.getElementById('start').value")
                        if value != "00:00:00.5":
                            raise RuntimeError(f"シーク位置が不一致です: {value}")
                        window.evaluate_js("document.getElementById('video').play()")
                        time.sleep(0.5)
                        played = window.evaluate_js("document.getElementById('video').currentTime > 0.7")
                        if not played:
                            raise RuntimeError("動画の再生が進みませんでした。")
                        events.put(("verified", "再生・シーク・範囲指定を確認"))
                        break
                    events.put(("ready", ""))
                    closing.wait()
                    break
            except Exception as error:  # noqa: BLE001 - communicate GUI initialization failures
                events.put(("error", str(error)))
            finally:
                window.destroy()

        webview.start(monitor, gui="edgechromium", private_mode=True)
    except Exception as error:  # noqa: BLE001 - report native runtime failures to Tk
        events.put(("error", f"アプリ内プレーヤーを起動できません: {error}"))


class EmbeddedPlayer:
    def __init__(self, url, smoke=False):
        context = mp.get_context("spawn")
        self.events = context.Queue()
        self.closing = context.Event()
        self.process = context.Process(target=player_worker, args=(url, self.events, self.closing, smoke))
        self.process.start()
        self.dead_polls = 0

    def poll(self):
        messages = []
        while True:
            try:
                messages.append(self.events.get_nowait())
            except queue.Empty:
                break
        if not self.process.is_alive():
            self.dead_polls += 1
        return messages

    @property
    def finished(self):
        return self.dead_polls >= 3

    def close(self):
        self.closing.set()
        self.process.join(timeout=3)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
        self.events.close()
        if not self.process.is_alive():
            self.process.close()


def smoke_player(path):
    from clip_downloader.preview import PreviewServer
    server = PreviewServer()
    player = EmbeddedPlayer(server.add(Path(path), str(Path(path).parent), {}), smoke=True)
    verified = False
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            for kind, message in player.poll():
                if kind == "error":
                    raise RuntimeError(message)
                if kind == "verified":
                    verified = True
            if player.finished:
                return verified
            time.sleep(0.1)
        return False
    finally:
        player.close()
        server.close()
