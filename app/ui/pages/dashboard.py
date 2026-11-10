"""Tableau de bord : état des services, compteurs, lancement du pipeline, dernières tâches."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from app.core.health import ServiceStatus
from app.ui import data
from app.ui.pages.base import Page
from app.ui.widgets import StatusDot, card, esc

RUN_FR = {"OK": "publication faite", "NOTHING": "rien de publié", "FAILED": "échec", "PREFLIGHT": "services indisponibles",
          "LOCKED": "déjà en cours", "INTERRUPTED": "interrompu", "RUNNING": "en cours"}

SERVICES = [("llm", "LM Studio"), ("ffmpeg", "FFmpeg"), ("tts", "Voix"), ("wikimedia", "Wikimedia"), ("memory", "Mémoire")]


class DashboardPage(Page):
    title = "Tableau de bord"

    def __init__(self, shell) -> None:
        super().__init__(shell)
        self.refresh_btn = QPushButton("Vérifier les services")
        self.fetch_btn = QPushButton("Récupérer les actualités")
        self.pipeline_btn = QPushButton("Lancer le pipeline…")
        self.pipeline_btn.setDefault(True)
        self.autopilot_btn = QPushButton("Tout générer et publier")
        self.autopilot_btn.setToolTip("Récupère les actualités, monte les meilleurs sujets et publie sur YouTube sans "
                                      "relecture. Seules les vidéos qui passent tous les contrôles sont publiées.")
        for b in (self.refresh_btn, self.fetch_btn, self.pipeline_btn, self.autopilot_btn):
            self.toolbar.addWidget(b)
        self.refresh_btn.clicked.connect(self.check_services)
        self.fetch_btn.clicked.connect(shell.fetch_news)
        self.pipeline_btn.clicked.connect(lambda: shell.open_pipeline_dialog())
        self.autopilot_btn.clicked.connect(lambda: shell.run_autopilot())
        self.runner.busy_changed.connect(lambda busy: self.autopilot_btn.setEnabled(not busy))

        grid = QGridLayout()
        self.dots: dict[str, StatusDot] = {}
        for i, (key, label) in enumerate(SERVICES):
            self.dots[key] = StatusDot(label)
            grid.addWidget(card(self.dots[key]), i // 3, i % 3)
        self.root.addLayout(grid)

        counters = QGridLayout()
        self.counters: dict[str, QLabel] = {}
        for i, (key, label) in enumerate([("stories_24h", "Sujets (24 h)"), ("to_review", "Scripts à relire"),
                                          ("videos_ready", "Vidéos prêtes"), ("videos_review", "Vidéos à revoir")]):
            num = QLabel("–")
            num.setObjectName("counter")
            self.counters[key] = num
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.addWidget(num)
            lay.addWidget(QLabel(label))
            counters.addWidget(card(box), 0, i)
        self.root.addLayout(counters)

        sched = QWidget()
        lay = QHBoxLayout(sched)
        self.schedule_label = QLabel("Planification : vérification…")
        self.schedule_label.setWordWrap(True)
        self.schedule_on = QPushButton("Activer")
        self.schedule_on.setToolTip("Crée la tâche Windows qui lance « Tout générer et publier » aux heures scheduler.times.")
        self.schedule_off = QPushButton("Désactiver")
        lay.addWidget(self.schedule_label, 1)
        lay.addWidget(self.schedule_on)
        lay.addWidget(self.schedule_off)
        self.schedule_on.clicked.connect(lambda: shell.set_schedule(True))
        self.schedule_off.clicked.connect(lambda: shell.set_schedule(False))
        self.root.addWidget(card(sched))

        self.root.addWidget(QLabel("<b>Dernières tâches</b>"))
        self.history = QListWidget()
        self.root.addWidget(self.history, 1)
        self.runner.finished.connect(self._on_finished)

    def refresh(self) -> None:
        c = data.counts(self.ctx)
        for key, lbl in self.counters.items():
            lbl.setText(str(getattr(c, key)))

    def check_services(self) -> None:
        for dot in self.dots.values():
            dot.set_state(None, "vérification…")
        for key, _ in SERVICES:
            self.runner.submit(f"État {key}", lambda h, k=key: self.shell.health_check(k),
                               on_done=self.set_status, exclusive=False)

    def set_schedule(self, st) -> None:
        if st.installed:
            text = (f"<b>Planification active</b> : chaque jour à {', '.join(st.times)} · prochain lancement "
                    f"{st.next_run:%d/%m à %H:%M}")
        else:
            text = f"<b>Planification inactive</b> (heures prévues : {', '.join(st.times)})"
        if st.last is not None:
            when = st.last.started_at.astimezone() if st.last.started_at.tzinfo else st.last.started_at
            text += (f"<br>Dernier lancement {when:%d/%m %H:%M} : {RUN_FR.get(st.last.status, st.last.status)}, "
                     f"{st.last.published} publiée(s) — {esc(st.last.message[:160])}")
        self.schedule_label.setText(text)
        self.schedule_on.setEnabled(not st.installed)
        self.schedule_off.setEnabled(st.installed)

    def set_status(self, st: ServiceStatus) -> None:
        dot = self.dots.get(st.key)
        if dot:
            dot.name.setText(f"<b>{st.label}</b>")
            dot.set_state(st.ok, st.detail, st.warning)

    def _on_finished(self, info, ok: bool, message: str) -> None:
        if not info.exclusive:
            return
        self.history.insertItem(0, f"{'✔' if ok else '✖'} {info.title}" + ("" if ok else f" — {message[:160]}"))
