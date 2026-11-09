"""Global pause shortcut.

RegisterHotKey binds to the thread's message queue, so the key has to be
registered and pumped on one thread that stays alive. The callback runs on that
thread, which means it must not block; toggling filtering only flips a flag,
and the tick thread syncs the firewall rules.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_P = 0x50
HOTKEY_ID = 1
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class Hotkey(threading.Thread):
    def __init__(self, callback, log=None) -> None:
        super().__init__(name="hotkey", daemon=True)
        self.callback = callback
        self.log = log or (lambda *_: None)
        self._stop_event = threading.Event()
        self._thread_id = 0

    def run(self) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.GetMessageW.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_P):
            self.log("hotkey: Ctrl+Alt+P is already taken")
            return
        self.log("hotkey: Ctrl+Alt+P toggles filtering")
        message = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception as exc:
                        self.log(f"hotkey callback failed: {type(exc).__name__}: {exc}")
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

