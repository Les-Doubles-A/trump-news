"""Transmet les logs Python à l'interface (écran Journal), depuis n'importe quel fil, via un signal Qt."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Signal


@dataclass(frozen=True)
class LogLine:
    ts: datetime
    level: str
    logger: str
    message: str


class LogBridge(QObject):
    line = Signal(object)    # LogLine


class QtLogHandler(logging.Handler):
    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.bridge = LogBridge()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("sqlalchemy"):
            return
        try:
            self.bridge.line.emit(LogLine(datetime.fromtimestamp(record.created), record.levelname, record.name,
                                          record.getMessage()))
        except RuntimeError:          # fenêtre déjà fermée : on ignore
            pass
