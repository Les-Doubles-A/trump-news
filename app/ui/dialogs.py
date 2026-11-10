"""Fenêtres de dialogue : lancement du pipeline, relecture d'un script, affichage d'un résultat."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from app.news.service import StoryRow
from app.pipeline.runner import PipelineOptions
from app.review.service import ScriptDetail
from app.ui.html_views import script_html
from app.ui.widgets import badge_html, esc
from app.video.templates import TEMPLATES


class HtmlDialog(QDialog):
    def __init__(self, title: str, html: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 640)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setHtml(html)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(view)
        lay.addWidget(buttons)


class PipelineDialog(QDialog):
    """Choix du sujet et des options avant de lancer la chaîne complète."""

    def __init__(self, stories: list[StoryRow], preselect: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lancer le pipeline")
        self.resize(720, 260)
        self.story = QComboBox()
        for r in stories:
            flag = f" [fact-check {r.verdict}]" if r.verdict else ""
            self.story.addItem(f"#{r.id} · {r.score * 100:.0f}% · {r.n_sources} src · texte {r.n_text} — {r.title[:80]}{flag}", r.id)
        if preselect is not None:
            i = self.story.findData(preselect)
            if i >= 0:
                self.story.setCurrentIndex(i)
        self.duration = QComboBox()
        self.duration.addItem("selon les réglages", None)
        for d in (20, 30, 45, 60, 90):
            self.duration.addItem(f"{d} s", d)
        self.template = QComboBox()
        self.template.addItem("auto (selon le style)", None)
        for name, t in TEMPLATES.items():
            self.template.addItem(f"{name} — {t.label}", name)
        self.preview = QCheckBox("Aperçu rapide 540x960 (jamais publié)")
        self.force = QCheckBox("Refaire le fact-check même s'il est en cache")
        hint = QLabel("Conseil : choisissez un sujet avec « texte 2 » ou plus. Les étapes s'enchaînent en arrière-plan ; "
                      "vous pouvez continuer à utiliser l'application.")
        hint.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Sujet", self.story)
        form.addRow("Durée", self.duration)
        form.addRow("Template", self.template)
        form.addRow("", self.preview)
        form.addRow("", self.force)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Lancer")
        buttons.button(QDialogButtonBox.Ok).setEnabled(bool(stories))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addWidget(buttons)

    def story_id(self) -> int | None:
        return self.story.currentData()

    def options(self) -> PipelineOptions:
        return PipelineOptions(duration_s=self.duration.currentData(), template=self.template.currentData(),
                               preview=self.preview.isChecked(), force_factcheck=self.force.isChecked())


class ReviewDialog(QDialog):
    """Relecture : chaque phrase à côté de son affirmation, de la citation exacte et de la source.
    Approuver exige la case « vérifié », et une note quand le fact-check est CAUTION."""

    APPROVE, REJECT = 1, 2

    def __init__(self, detail: ScriptDetail, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.detail = detail
        self.decision: int | None = None
        self.setWindowTitle(f"Relire le script #{detail.id}")
        self.resize(900, 760)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setHtml(script_html(detail))
        self.checked = QCheckBox("J'ai vérifié les citations et les sources (chaque phrase dit bien ce que dit sa source)")
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Note : ce que vous avez vérifié, ou la raison d'un refus"
                                     + (" (obligatoire pour approuver : fact-check CAUTION)" if self.note_required else ""))
        self.note.setFixedHeight(80)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #d64545;")
        self.error.setWordWrap(True)
        info = QLabel(f"Fact-check : {badge_html(detail.verdict)} · statut actuel : {badge_html(detail.status)}. "
                      "L'approbation ne lève que le contrôle « script validé » ; les contrôles techniques des vidéos restent "
                      "bloquants.")
        info.setWordWrap(True)
        self.approve_btn = QPushButton("Approuver")
        self.reject_btn = QPushButton("Refuser")
        cancel = QPushButton("Fermer")
        self.approve_btn.clicked.connect(self._approve)
        self.reject_btn.clicked.connect(self._reject)
        cancel.clicked.connect(self.reject)
        if detail.verdict == "BLOCKED":
            self.approve_btn.setEnabled(False)
            self.approve_btn.setToolTip("Sujet BLOCKED par le fact-check : approbation impossible.")
        row = QHBoxLayout()
        row.addWidget(self.error, 1)
        row.addWidget(self.reject_btn)
        row.addWidget(self.approve_btn)
        row.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.addWidget(view, 1)
        lay.addWidget(info)
        lay.addWidget(self.checked)
        lay.addWidget(self.note)
        lay.addLayout(row)

    @property
    def note_required(self) -> bool:
        return self.detail.verdict in (None, "CAUTION")

    def note_text(self) -> str:
        return self.note.toPlainText().strip()

    def _approve(self) -> None:
        if not self.checked.isChecked():
            self.error.setText("Cochez d'abord « J'ai vérifié les citations et les sources ».")
            return
        if self.note_required and not self.note_text():
            self.error.setText(f"Note obligatoire : le fact-check est {esc(self.detail.verdict or 'introuvable')}.")
            return
        self.decision = self.APPROVE
        self.accept()

    def _reject(self) -> None:
        self.decision = self.REJECT
        self.accept()
