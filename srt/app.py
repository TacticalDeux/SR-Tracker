"""App entry point. Wires the modules together and starts the Qt event loop."""
from __future__ import annotations

import sys

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import paths, theme
from .db import Database
from .dll import TrackerDLL
from .main_window import MainWindow
from .settings import SettingsStore


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("SR Tracker")
    # Keep the process alive when the main window is minimized; the tray
    # icon + overlay both need to keep running.
    app.setQuitOnLastWindowClosed(False)
    theme.apply(app)

    # Database — dev vs frozen location.
    db_path = paths.user_db_path() if getattr(sys, "frozen", False) else paths.dev_db_path()
    db = Database(db_path)

    # Settings store (in the same user data dir as the DB when frozen).
    settings_store = SettingsStore()

    # DLL — required. If it's missing, surface a clear error and exit.
    try:
        dll = TrackerDLL()
    except FileNotFoundError as e:
        QMessageBox.critical(
            None, "DLL missing",
            f"sr_tracker.dll not found at {paths.dll_path()}\n\n{e}",
        )
        return 1

    win = MainWindow(dll, db, settings_store)
    win.show()

    _install_tray(app, win)

    return app.exec()


def _install_tray(app: QApplication, win: MainWindow) -> None:
    tray = QSystemTrayIcon(QIcon(), app)
    tray.setToolTip("SR Tracker")
    menu = QMenu()

    act_overlay = QAction("Toggle Overlay", app)
    act_overlay.triggered.connect(win._toggle_overlay)
    menu.addAction(act_overlay)

    act_boosts = QAction("Toggle Boosts", app)
    act_boosts.triggered.connect(win._toggle_boosts)
    menu.addAction(act_boosts)

    act_show = QAction("Show Main Window", app)
    act_show.triggered.connect(win.show)
    menu.addAction(act_show)

    menu.addSeparator()

    act_quit = QAction("Quit", app)
    # Closing the main window is the canonical quit gesture, so route
    # the tray's "Quit" through the same path: close the window, which
    # runs closeEvent (stops the consumer, uninstalls hooks, releases
    # the DB) and then quits the app.
    act_quit.triggered.connect(win.close)
    menu.addAction(act_quit)


if __name__ == "__main__":
    sys.exit(main())
