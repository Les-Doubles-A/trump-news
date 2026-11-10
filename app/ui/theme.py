"""Thème : suit le réglage clair/sombre de Windows (style Fusion + palette), et se met à jour s'il change."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

COLORS = {   # couleurs d'état, lisibles sur les deux fonds
    "ok": "#2e9d5b", "warn": "#d08a00", "error": "#d64545", "info": "#3a7bd5", "muted": "#8a8f98",
}


def is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _palette(dark: bool) -> QPalette:
    p = QPalette()
    if dark:
        base, alt, text, window, button, hl = "#1e1f22", "#26282c", "#e6e6e6", "#2b2d31", "#33363b", "#3a7bd5"
    else:
        base, alt, text, window, button, hl = "#ffffff", "#f4f5f7", "#1d1f23", "#f0f1f3", "#e9eaee", "#2f6fd0"
    for role, col in [(QPalette.Window, window), (QPalette.WindowText, text), (QPalette.Base, base),
                      (QPalette.AlternateBase, alt), (QPalette.Text, text), (QPalette.Button, button),
                      (QPalette.ButtonText, text), (QPalette.Highlight, hl), (QPalette.HighlightedText, "#ffffff"),
                      (QPalette.ToolTipBase, base), (QPalette.ToolTipText, text), (QPalette.PlaceholderText, COLORS["muted"]),
                      (QPalette.Link, hl)]:
        p.setColor(role, QColor(col))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(COLORS["muted"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(COLORS["muted"]))
    return p


STYLE = """
QListWidget#nav { border: none; font-size: 14px; padding-top: 8px; outline: 0; }
QListWidget#nav::item { padding: 10px 14px; border-radius: 6px; margin: 2px 6px; }
QListWidget#nav::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QLabel#title { font-size: 20px; font-weight: 600; }
QLabel#counter { font-size: 26px; font-weight: 600; }
QFrame#card { border: 1px solid palette(mid); border-radius: 8px; }
QPushButton { padding: 6px 12px; }
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    if "Segoe UI" in QFontDatabase.families():          # police de Windows, lisible à toutes les tailles
        app.setFont(QFont("Segoe UI", 10))
    app.setPalette(_palette(is_dark()))
    app.setStyleSheet(STYLE)
    try:
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: app.setPalette(_palette(is_dark())))
    except AttributeError:      # Qt < 6.5 : thème fixe
        pass
