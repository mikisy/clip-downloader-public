"""Shared video actions for search results and source-link candidates."""

from tkinter import Menu


class VideoContextMenu:
    def __init__(self, table, queue, add, sources, open_video, queue_enabled=True):
        self.table = table
        self.queue_enabled = queue_enabled
        self.menu = Menu(table, tearoff=False)
        for label, command in (("DLキューに追加", queue), ("DL欄に追加", add),
                               ("元動画URLを確認", sources), ("YouTubeで開く", open_video)):
            self.menu.add_command(label=label, command=command)
        table.bind("<Button-3>", self.popup)

    def popup(self, event):
        if self.table.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        row = self.table.identify_row(event.y)
        if not row:
            return
        if row not in self.table.selection():
            self.table.selection_set(row)
        self.table.focus(row)
        self.table.focus_set()
        # Deliver selection callbacks before any menu command reads the URL field.
        self.table.event_generate("<<TreeviewSelect>>")
        count = len(self.table.selection())
        self.menu.entryconfigure(0, state="normal" if self.queue_enabled else "disabled")
        for index in (2, 3):
            self.menu.entryconfigure(index, state="normal" if count == 1 else "disabled")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"
