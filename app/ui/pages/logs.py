"""Journal : logs en direct (et les 300 derniers enregistrés en base au démarrage), filtrables par niveau."""
from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QComboBox, QPlainTextEdit, QPushButton

from app.database.session import latest_logs
from app.ui.log_bridge import LogLine
from app.ui.pages.base import Page

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class LogsPage(Page):
    title = "Journal"

    def __init__(self, shell) -> None:
        super().__init__(shell)
        self.level = QComboBox()
        for name, label in (("INFO", "Tout"), ("WARNING", "Avertissements et erreurs"), ("ERROR", "Erreurs seulement")):
            self.level.addItem(label, name)
        clear = QPushButton("Effacer l'affichage")
        self.toolbar.addWidget(self.level)
        self.toolbar.addWidget(clear)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setFont(QFont("Consolas", 9))
        self.root.addWidget(self.view, 1)
        self.lines: list[LogLine] = []
        self.level.currentIndexChanged.connect(self._redraw)
        clear.clicked.connect(self._clear)
        for e in reversed(latest_logs(self.ctx.factory, 300)):
            self.lines.append(LogLine(e.ts, e.level, e.logger, e.message))
        self._redraw()

    def add_line(self, line: LogLine) -> None:
        self.lines.append(line)
        del self.lines[:-5000]
        if self._visible(line):
            self.view.appendPlainText(self._fmt(line))

    def _visible(self, line: LogLine) -> bool:
        return LEVELS.get(line.level, 20) >= LEVELS[self.level.currentData()]

    @staticmethod
    def _fmt(line: LogLine) -> str:
        return f"{line.ts:%H:%M:%S} {line.level:<7} {line.message}"

    def _redraw(self) -> None:
        self.view.setPlainText("\n".join(self._fmt(x) for x in self.lines if self._visible(x)))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _clear(self) -> None:
        self.lines.clear()
        self.view.clear()
