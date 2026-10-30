"""Tray icon so closing the window keeps filtering alive.

pystray is optional at runtime: if it or Pillow is missing the app falls back
to a normal close instead of trapping the user with no way to quit.
"""

from __future__ import annotations

import threading
from typing import Callable


def available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image, ImageDraw  # noqa: F401
    except Exception:
        return False
    return True


def _icon_image():
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(24, 100, 171, 255))
    draw.ellipse((16, 16, size - 16, size - 16), outline=(255, 255, 255, 255), width=4)
    draw.line((32, 10, 32, 20), fill=(255, 255, 255, 255), width=3)
    draw.line((32, 44, 32, 54), fill=(255, 255, 255, 255), width=3)
    draw.line((10, 32, 20, 32), fill=(255, 255, 255, 255), width=3)
    draw.line((44, 32, 54, 32), fill=(255, 255, 255, 255), width=3)
    return image


class TrayController:
    def __init__(
        self,
        open_window: Callable[[], None],
        toggle_filtering: Callable[[bool], None],
        quit_app: Callable[[], None],
        filtering: bool = True,
    ) -> None:
        self._open_window = open_window
        self._toggle_filtering = toggle_filtering
        self._quit_app = quit_app
        self._filtering = filtering
        self._icon = None
        self._stopped = False
        self._lock = threading.Lock()

    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem("Open COD Ping King", self._on_open, default=True),
            pystray.MenuItem(
                "Filtering",
                self._on_toggle,
                checked=lambda item: self._filtering,
            ),
            pystray.MenuItem("Quit", self._on_quit),
        )

    def show(self) -> None:
        with self._lock:
            if self._stopped:
                return
            if self._icon is None:
                import pystray

                self._icon = pystray.Icon(
                    "cod-ping-king",
                    _icon_image(),
                    "COD Ping King",
                    self._menu(),
                )
                self._icon.run_detached()
                return
            self._icon.visible = True

    def hide(self) -> None:
        with self._lock:
            if self._icon is not None:
                self._icon.visible = False

    def set_filtering(self, filtering: bool) -> None:
        self._filtering = filtering
        if self._icon is not None:
            self._icon.update_menu()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            icon, self._icon = self._icon, None
        if icon is not None:
            icon.stop()

    def _on_open(self, icon=None, item=None) -> None:
        self._open_window()

    def _on_toggle(self, icon=None, item=None) -> None:
        self._filtering = not self._filtering
        self._toggle_filtering(self._filtering)
        if self._icon is not None:
            self._icon.update_menu()

    def _on_quit(self, icon=None, item=None) -> None:
        self._quit_app()
