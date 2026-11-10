"""Démarrage de l'interface graphique : `run.bat ui` (ou `python -m app.ui`)."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.context import AppContext
from app.core.errors import TrumpNewsError
from app.ui.log_bridge import QtLogHandler
from app.ui.main_window import MainWindow
from app.ui.theme import apply_theme


def run_ui(config_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Trump News")
    apply_theme(app)
    handler = QtLogHandler()
    try:
        ctx = AppContext.load(config_path, extra_log_handlers=(handler,))
    except TrumpNewsError as exc:
        QMessageBox.critical(None, "Trump News — configuration invalide",
                             f"{exc}\n\nCorrigez le réglage (run.bat set-option …) ou recréez la configuration "
                             "(run.bat reset-config).")
        return 2
    win = MainWindow(ctx, log_handler=handler)
    win.show()
    win.start()
    return app.exec()
