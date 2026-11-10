"""Scripts : liste, détail (phrases, affirmations, citations, sources), relecture, montage de la vidéo."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QPushButton, QSplitter, QTextBrowser

from app.core.errors import TrumpNewsError
from app.ui import data
from app.ui.dialogs import ReviewDialog
from app.ui.html_views import script_html
from app.ui.pages.base import Page
from app.ui.widgets import fill_table, make_table, select_key, selected_key


class ScriptsPage(Page):
    title = "Scripts"

    def __init__(self, shell) -> None:
        super().__init__(shell)
        self.only_review = QCheckBox("Seulement « à relire »")
        self.review_btn = QPushButton("Relire…")
        self.produce_btn = QPushButton("Voix + visuels + montage")
        self.preview = QCheckBox("en aperçu")
        for w in (self.only_review, self.review_btn, self.produce_btn, self.preview):
            self.toolbar.addWidget(w)
        self.only_review.toggled.connect(self.refresh)
        self.review_btn.clicked.connect(self.open_review)
        self.produce_btn.clicked.connect(self._produce)

        self.table = make_table(["#", "Sujet", "Statut", "Style", "Durée", "Accroche"], stretch_col=5)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.doubleClicked.connect(lambda *_: self.open_review())
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([480, 620])
        self.root.addWidget(split, 1)
        self._stories: dict[int, int] = {}

    def refresh(self) -> None:
        rows = [r for r in data.scripts(self.ctx) if not self.only_review.isChecked() or r.status == "NEEDS_REVIEW"]
        self._stories = {r.id: r.story_id for r in rows}
        fill_table(self.table, [(r.id, r.story_id, r.status, r.style, f"{r.duration_target} s", r.hook or "") for r in rows],
                   status_col=2)
        self._show_detail()

    def select(self, key) -> None:
        select_key(self.table, key)

    def _show_detail(self) -> None:
        sid = selected_key(self.table)
        for b in (self.review_btn, self.produce_btn):
            b.setEnabled(sid is not None)
        if sid is None:
            self.detail.setHtml("<i>Sélectionnez un script.</i>")
            return
        try:
            self.detail.setHtml(script_html(self.ctx.reviews().detail(sid)))
        except TrumpNewsError as exc:
            self.detail.setHtml(f"<i>{exc}</i>")

    def open_review(self, dialog_factory=ReviewDialog) -> None:
        sid = selected_key(self.table)
        if sid is None:
            return
        dlg = dialog_factory(self.ctx.reviews().detail(sid), self)
        if not dlg.exec() or dlg.decision is None:
            return
        self.shell.submit_review(sid, approve=dlg.decision == ReviewDialog.APPROVE, note=dlg.note_text(),
                                 sources_checked=dlg.checked.isChecked())

    def _produce(self) -> None:
        sid = selected_key(self.table)
        if sid is not None:
            self.shell.produce_video(sid, self._stories.get(sid, 0), preview=self.preview.isChecked())
