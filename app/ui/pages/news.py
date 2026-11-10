"""Actualités : sujets classés, détail du score, récupération, fact-check, pipeline."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QPushButton, QSplitter, QTextBrowser

from app.ui.pages.base import Page
from app.ui.widgets import esc, fill_table, make_table, select_key, selected_key
from app.utils.timeutil import as_utc, utcnow


class NewsPage(Page):
    title = "Actualités"

    def __init__(self, shell) -> None:
        super().__init__(shell)
        self.show_used = QCheckBox("Afficher les sujets déjà traités")
        self.fetch_btn = QPushButton("Récupérer les actualités")
        self.fc_btn = QPushButton("Fact-check")
        self.used_btn = QPushButton("Marquer comme traité")
        self.pipe_btn = QPushButton("Pipeline complet…")
        for w in (self.show_used, self.fetch_btn, self.fc_btn, self.used_btn, self.pipe_btn):
            self.toolbar.addWidget(w)
        self.show_used.toggled.connect(self.refresh)
        self.fetch_btn.clicked.connect(shell.fetch_news)
        self.fc_btn.clicked.connect(self._fact_check)
        self.used_btn.clicked.connect(self._mark_used)
        self.pipe_btn.clicked.connect(lambda: shell.open_pipeline_dialog(selected_key(self.table)))

        self.table = make_table(["#", "Score", "Sources", "Texte", "Âge", "Catégorie", "Fact-check", "Titre"], stretch_col=7)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.doubleClicked.connect(lambda *_: shell.open_pipeline_dialog(selected_key(self.table)))
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([500, 160])
        self.root.addWidget(split, 1)
        self._rows = {}

    def refresh(self) -> None:
        svc, _ = self.ctx.news_service()
        rows = svc.list_stories(limit=200, include_used=self.show_used.isChecked())
        now = utcnow()
        self._rows = {r.id: r for r in rows}

        def age(r):
            h = (now - as_utc(r.newest)).total_seconds() / 3600
            return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} j"

        fill_table(self.table, [(r.id, f"{r.score * 100:.0f}%", r.n_sources, r.n_text, age(r), r.category or "-",
                                 r.verdict or "", r.title + ("  [traité]" if r.used else "")) for r in rows], status_col=6)
        self._show_detail()

    def select(self, key) -> None:
        select_key(self.table, key)

    def _show_detail(self) -> None:
        r = self._rows.get(selected_key(self.table))
        self.fc_btn.setEnabled(r is not None)
        self.used_btn.setEnabled(r is not None and not r.used)
        self.pipe_btn.setEnabled(r is not None)
        if r is None:
            self.detail.setHtml("<i>Sélectionnez un sujet.</i>")
            return
        comp = " · ".join(f"{esc(k)} {v}" for k, v in r.breakdown.components.items())
        self.detail.setHtml(f"<b>{esc(r.title)}</b><br>Sources : {esc(', '.join(r.sources))}<br>"
                            f"Articles : {r.n_articles} (dont {r.n_text} avec résumé ou texte)<br>Score : {comp}")

    def _fact_check(self) -> None:
        sid = selected_key(self.table)
        if sid is None:
            return
        self.shell.run_fact_check(sid)

    def _mark_used(self) -> None:
        from app.news.service import mark_story_used

        sid = selected_key(self.table)
        if sid is not None and mark_story_used(self.ctx.factory, sid):
            self.refresh()
