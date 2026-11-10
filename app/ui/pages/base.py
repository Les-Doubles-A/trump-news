"""Base des écrans : accès au contexte, à la file de tâches et aux notifications de la fenêtre principale."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


class Page(QWidget):
    title = ""

    def __init__(self, shell: "MainWindow") -> None:
        super().__init__()
        self.shell = shell
        self.ctx = shell.ctx
        self.runner = shell.runner
        self.notify = shell.notify
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(18, 14, 18, 14)
        head = QLabel(self.title)
        head.setObjectName("title")
        self.toolbar = QHBoxLayout()
        top = QHBoxLayout()
        top.addWidget(head)
        top.addStretch(1)
        top.addLayout(self.toolbar)
        self.root.addLayout(top)

    def refresh(self) -> None:
        """Relit les données (appelé à l'affichage de l'écran et après chaque tâche terminée)."""

    def select(self, key) -> None:
        """Sélectionne un élément (navigation depuis un autre écran)."""
