"""Vidéos : lecteur intégré, contrôle qualité, crédits, nouveau contrôle, remontage."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSplitter, QTextBrowser,
                               QVBoxLayout, QWidget)

from app.ui import data
from app.ui.html_views import quality_html
from app.ui.pages.base import Page
from app.ui.widgets import fill_table, local_time, make_table, open_path, select_key, selected_key
from app.video.service import latest_report
from app.video.templates import TEMPLATES


class VideoPlayer(QWidget):
    """Lecteur Qt Multimedia ; s'il n'est pas disponible, seul le bouton « Ouvrir dans le lecteur » reste."""

    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self.path: Path | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.player = None
        if enabled:
            try:
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
                from PySide6.QtMultimediaWidgets import QVideoWidget
                self.view = QVideoWidget()
                self.view.setMinimumSize(270, 480)
                self.player = QMediaPlayer(self)
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
                self.player.setVideoOutput(self.view)
                lay.addWidget(self.view, 1)
            except Exception:          # multimédia indisponible sur ce PC : repli sur le lecteur du système
                self.player = None
        if self.player is None:
            msg = QLabel("Lecteur intégré indisponible : utilisez « Ouvrir dans le lecteur ».")
            msg.setWordWrap(True)
            lay.addWidget(msg, 1)
        row = QHBoxLayout()
        self.play_btn = QPushButton("Lecture / pause")
        self.open_btn = QPushButton("Ouvrir dans le lecteur")
        self.folder_btn = QPushButton("Ouvrir le dossier")
        self.play_btn.setEnabled(self.player is not None)
        for b in (self.play_btn, self.open_btn, self.folder_btn):
            row.addWidget(b)
        lay.addLayout(row)
        self.play_btn.clicked.connect(self.toggle)
        self.open_btn.clicked.connect(lambda: self.path and open_path(self.path))
        self.folder_btn.clicked.connect(lambda: self.path and open_path(self.path.parent))

    def load(self, path: Path | None) -> None:
        self.path = path if path and path.exists() else None
        for b in (self.open_btn, self.folder_btn):
            b.setEnabled(self.path is not None)
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(self.path)) if self.path else QUrl())

    def toggle(self) -> None:
        if self.player is None or self.path is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()


class VideosPage(Page):
    title = "Vidéos"

    def __init__(self, shell, *, player: bool = True) -> None:
        super().__init__(shell)
        self.check_btn = QPushButton("Refaire le contrôle")
        self.template = QComboBox()
        self.template.addItem("template : auto", None)
        for name in TEMPLATES:
            self.template.addItem(f"template : {name}", name)
        self.preview = QCheckBox("en aperçu")
        self.rerender_btn = QPushButton("Remonter")
        self.publish_btn = QPushButton("Publier…")
        self.publish_btn.setToolTip("Publie la vidéo sélectionnée sur les réseaux activés (YouTube, TikTok, Instagram), "
                                    "après contrôle des garde-fous.")
        for w in (self.check_btn, self.template, self.preview, self.rerender_btn, self.publish_btn):
            self.toolbar.addWidget(w)
        self.check_btn.clicked.connect(self._recheck)
        self.publish_btn.clicked.connect(self._publish)
        self.rerender_btn.clicked.connect(self._rerender)

        self.table = make_table(["#", "Script", "Statut", "Template", "Durée", "Qualité", "Créée"], stretch_col=6)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.player = VideoPlayer(enabled=player)
        self.report = QTextBrowser()
        right = QSplitter(Qt.Horizontal)
        right.addWidget(self.player)
        right.addWidget(self.report)
        right.setSizes([320, 520])
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([220, 520])
        self.root.addWidget(split, 1)
        self._rows = {}

    def refresh(self) -> None:
        rows = data.videos(self.ctx)
        self._rows = {r.id: r for r in rows}
        fill_table(self.table, [(r.id, r.script_id, r.status, r.template or "-", f"{r.duration:.1f} s" if r.duration else "-",
                                 f"{r.quality_score:.2f}" if r.quality_score is not None else "-",
                                 local_time(r.created_at)) for r in rows], status_col=2)
        self._show_detail()

    def select(self, key) -> None:
        select_key(self.table, key)

    def _show_detail(self) -> None:
        vid = selected_key(self.table)
        row = self._rows.get(vid)
        for b in (self.check_btn, self.rerender_btn, self.publish_btn):
            b.setEnabled(row is not None)
        if row is None:
            self.player.load(None)
            self.report.setHtml("<i>Sélectionnez une vidéo.</i>")
            return
        self.player.load(Path(row.video_path) if row.video_path else None)
        self.report.setHtml(quality_html(latest_report(self.ctx.factory, vid)))

    def _recheck(self) -> None:
        vid = selected_key(self.table)
        if vid is not None:
            self.shell.recheck_video(vid)

    def _publish(self) -> None:
        vid = selected_key(self.table)
        if vid is not None:
            self.shell.publish_video(vid)

    def _rerender(self) -> None:
        row = self._rows.get(selected_key(self.table))
        if row is not None:
            self.player.stop()
            self.shell.render_video(row.script_id, template=self.template.currentData(), preview=self.preview.isChecked())
