"""Native Windows folder/file dialogs invoked from the local web UI."""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()


def _root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    return root


def pick_folder(initial: str = "") -> str:
    with _lock:
        root = _root()
        try:
            from tkinter import filedialog
            path = filedialog.askdirectory(initialdir=initial or None, mustexist=False,
                                           title="选择工作区目录")
        finally:
            root.destroy()
    return os.path.normpath(path) if path else ""


def pick_file(initial: str = "", kind: str = "exe") -> str:
    with _lock:
        root = _root()
        try:
            from tkinter import filedialog
            types = [("可执行文件", "*.exe;*.cmd;*.bat"), ("所有文件", "*.*")] \
                if kind == "exe" else [("所有文件", "*.*")]
            path = filedialog.askopenfilename(initialdir=initial or None,
                                              title="选择程序", filetypes=types)
        finally:
            root.destroy()
    return os.path.normpath(path) if path else ""
