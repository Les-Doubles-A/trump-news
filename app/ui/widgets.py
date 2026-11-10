"""Petits composants partagés par les écrans."""
from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app.ui.theme import COLORS

STATUS_COLOR = {
    "READY": "ok", "SAFE": "ok", "APPROVED": "ok", "DRAFT": "info", "CONFIRMED": "ok", "LIKELY": "ok",
    "NEEDS_REVIEW": "warn", "CAUTION": "warn", "REPORTED": "warn", "RENDERING": "info",
    "BLOCKED": "error", "FAILED": "error", "REJECTED": "error", "DISPUTED": "error",
}


def color_for(status: str | None) -> str:
    return COLORS[STATUS_COLOR.get((status or "").upper(), "muted")]


def local_time(dt, fmt: str = "%d/%m %H:%M") -> str:
    """Les dates sont stockées en UTC : affichage à l'heure locale du PC."""
    if dt is None:
        return ""
    from app.utils.timeutil import as_utc
    return as_utc(dt).astimezone().strftime(fmt)


def esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def badge_html(status: str | None) -> str:
    return f'<span style="color:{color_for(status)}; font-weight:600">{esc(status or "-")}</span>'


class StatusDot(QWidget):
    """Pastille de couleur + libellé + détail : état d'un service."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.dot = QLabel("●")
        self.name = QLabel(f"<b>{esc(label)}</b>")
        self.detail = QLabel("vérification…")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top = QHBoxLayout()
        top.addWidget(self.dot)
        top.addWidget(self.name)
        top.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.addLayout(top)
        lay.addWidget(self.detail)
        self.set_state(None, "vérification…")

    def set_state(self, ok: bool | None, detail: str, warning: bool = False) -> None:
        key = "muted" if ok is None else ("error" if not ok else ("warn" if warning else "ok"))
        self.dot.setStyleSheet(f"color: {COLORS[key]}; font-size: 16px;")
        self.detail.setText(detail)
        self.detail.setToolTip(detail)
        self.state = key


def card(*widgets: QWidget) -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    for w in widgets:
        lay.addWidget(w)
    return f


def make_table(headers: Sequence[str], stretch_col: int | None = None) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.setSortingEnabled(False)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch_col is not None:
        hh.setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return t


def fill_table(t: QTableWidget, rows: Sequence[Sequence[Any]], *, key_col: int = 0, status_col: int | None = None,
               keys: Sequence[Any] | None = None) -> None:
    """Remplit le tableau ; la clé de chaque ligne (id) est stockée dans la première cellule (Qt.UserRole)."""
    selected = selected_key(t)
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            item = QTableWidgetItem("" if val is None else str(val))
            if isinstance(val, (int, float)):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if c == status_col:
                item.setForeground(QColor(color_for(str(val))))
            t.setItem(r, c, item)
        t.item(r, 0).setData(Qt.UserRole, keys[r] if keys is not None else row[key_col])
    if selected is not None:
        select_key(t, selected)


def selected_key(t: QTableWidget) -> Any:
    rows = t.selectionModel().selectedRows() if t.selectionModel() else []
    if not rows:
        return None
    item = t.item(rows[0].row(), 0)
    return item.data(Qt.UserRole) if item else None


def select_key(t: QTableWidget, key: Any) -> bool:
    for r in range(t.rowCount()):
        item = t.item(r, 0)
        if item is not None and item.data(Qt.UserRole) == key:
            t.selectRow(r)
            return True
    return False


def open_path(path: str | Path) -> None:
    """Ouvre un fichier ou un dossier avec le programme par défaut du système."""
    p = str(path)
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", p])


class Notifier:
    """Messages à l'utilisateur ; remplaçable dans les tests (aucune boîte modale)."""

    def __init__(self, parent: QWidget | None = None, quiet: bool = False) -> None:
        self.parent, self.quiet, self.history = parent, quiet, []

    def error(self, title: str, message: str) -> None:
        self.history.append(("error", title, message))
        if not self.quiet:
            QMessageBox.warning(self.parent, title, message)

    def info(self, title: str, message: str) -> None:
        self.history.append(("info", title, message))
        if not self.quiet:
            QMessageBox.information(self.parent, title, message)

    def confirm(self, title: str, message: str) -> bool:
        self.history.append(("confirm", title, message))
        if self.quiet:
            return True
        return QMessageBox.question(self.parent, title, message) == QMessageBox.Yes
