"""Tint the native Windows title bar to match the selected theme."""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger("cod-ping-king")

TITLE = "COD Ping King"

# caption, text, border, dark fallback for Windows 10
THEMES = {
    "midnight": ("#12151a", "#e8edf4", "#232931", True),
    "onyx": ("#121214", "#ececee", "#26262b", True),
    "ember": ("#141110", "#f1eae3", "#29221e", True),
    "light": ("#f3f5f8", "#151b23", "#dfe4ec", False),
}

LEGACY = {
    "dark-blue": "midnight",
    "dark-neutral": "onyx",
    "dark-coffee": "ember",
}


def normalise(name: str) -> str:
    """Map names saved by 1.0.2 previews to the released ones."""
    return LEGACY.get(name, name)

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def _colorref(value: str) -> int:
    value = value.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return r | (g << 8) | (b << 16)


def apply(theme: str, title: str = TITLE) -> bool:
    """Paint the caption and border; Windows 10 keeps its own frame color."""
    theme = normalise(theme)
    if sys.platform != "win32" or theme not in THEMES:
        return False
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if not hwnd:
            return False
        caption, text, border, dark = THEMES[theme]
        attributes = (
            (DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0),
            (DWMWA_CAPTION_COLOR, _colorref(caption)),
            (DWMWA_TEXT_COLOR, _colorref(text)),
            (DWMWA_BORDER_COLOR, _colorref(border)),
        )
        for attribute, value in attributes:
            data = ctypes.c_int(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)
            )
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        return True
    except Exception:
        log.debug("title bar tint failed for %s", theme, exc_info=True)
        return False
