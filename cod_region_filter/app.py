"""Entry point.

    python -m cod_region_filter            normal launch
    python -m cod_region_filter --preview  open the UI with sample data
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import paths
from .titlebar import TITLE as WINDOW_TITLE, apply as apply_titlebar

MUTEX_NAME = "Local\\CODPingKing.SingleInstance"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def _activate_existing() -> bool:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """One process only: two copies would fight over the driver and catalog."""
    global _mutex_handle
    # use_last_error and an explicit HANDLE restype matter here: the default
    # int restype truncates the handle and get_last_error is the only reliable
    # way to see ERROR_ALREADY_EXISTS after CreateMutexW.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    if not handle or already:
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex_handle = handle
    return True


def setup_logging(verbose: bool) -> logging.Logger:
    paths.local_data_dir().mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [handler]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("cod-ping-king")



def hard_exit(state) -> None:
    """Shut workers down, then leave immediately.

    pywebview teardown from a tray or API thread can hang; process exit releases
    the WinDivert handle, which flushes queued packets, so this still fails open.
    """
    try:
        state.shutdown()
    finally:
        logging.shutdown()
        os._exit(0)


def preview() -> int:
    target = Path(__file__).parent / "ui" / "index.html"
    webbrowser.open(target.as_uri() + "?demo=1")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cod-ping-king")
    parser.add_argument("--preview", action="store_true", help="open the interface with sample data")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--minimized", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.preview:
        return preview()

    log = setup_logging(args.verbose)

    if not acquire_single_instance():
        if _activate_existing():
            log.info("another instance is running; activated it")
        else:
            log.info("another instance is running")
        return 0

    try:
        import webview
    except Exception:
        log.error("pywebview is not installed")
        print(
            "pywebview is required for the desktop window.\n"
            "  pip install -r requirements.txt\n"
            "Or preview the interface with: python -m cod_region_filter --preview",
            file=sys.stderr,
        )
        return 2

    from .api import Api
    from . import tray as tray_mod
    from .state import AppState, clear_legacy_startup

    clear_legacy_startup()
    state = AppState(log=log.info)
    state.start()
    api = Api(state)

    # A logon launch must not flash a window; --minimized starts hidden and
    # only the tray icon appears. Without pystray, show the window instead of
    # starting invisible with no way back.
    start_hidden = bool(args.minimized and tray_mod.available())
    window = webview.create_window(
        WINDOW_TITLE,
        url=(paths.ui_dir() / "index.html").as_uri(),
        js_api=api,
        width=1240,
        height=820,
        min_size=(1040, 700),
        background_color="#101215",
        text_select=True,
        hidden=start_hidden,
    )

    def on_closed() -> None:
        state.shutdown()
        if tray_ctl is not None:
            tray_ctl.stop()
        log.info("window closed")

    def show_window() -> None:
        if quitting:
            return
        window.show()
        window.restore()

    def toggle_filtering(on: bool) -> None:
        state.set_setting("filtering", bool(on))

    def quit_app() -> None:
        nonlocal quitting
        if quitting:
            return
        quitting = True
        # Do not tear down from pystray's callback thread: stopping the icon
        # from inside its own menu loop can block, which is why Quit used to
        # leave the process alive. A worker thread owns the shutdown instead.
        threading.Thread(target=quit_now, name="quit", daemon=True).start()

    def quit_now() -> None:
        log.info("quit requested from tray")
        if tray_ctl is not None:
            tray_ctl.stop()
            # Give the shell a moment to remove the icon before the process
            # disappears; otherwise Windows can leave a ghost icon behind.
            time.sleep(0.3)
        hard_exit(state)
    tray_ctl = None
    quitting = False
    if tray_mod.available():
        tray_ctl = tray_mod.TrayController(
            show_window,
            toggle_filtering,
            quit_app,
            filtering=bool(state.config.get("filtering", True)),
        )

    def on_closing():
        if quitting:
            return True
        if state.config.get("minimize_to_tray") and tray_ctl is not None:
            window.hide()
            tray_ctl.show()
            log.info("minimized to tray")
            return False
        return True

    def after_start() -> None:
        apply_titlebar(state.config.get("theme", "midnight"))
        if start_hidden and tray_ctl is not None:
            window.hide()
            tray_ctl.show()

    window.events.closed += on_closed
    window.events.closing += on_closing
    try:
        webview.start(func=after_start, debug=args.debug)
    finally:
        state.shutdown()
        if tray_ctl is not None:
            tray_ctl.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
