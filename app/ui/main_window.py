"""Fenêtre principale : navigation, barre de tâche (progression, annulation) et actions partagées par les écrans.

Les actions longues passent toutes par JobRunner (une à la fois) ; leurs résultats reviennent ici dans le fil de
l'interface, qui rafraîchit l'écran affiché.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QProgressBar, QPushButton,
                               QStackedWidget, QWidget)

from app.core import health
from app.core.context import AppContext
from app.core.health import ServiceStatus
from app.pipeline.runner import PipelineOptions, PipelineResult, PipelineRunner
from app.ui.dialogs import HtmlDialog, PipelineDialog
from app.ui.html_views import factcheck_html
from app.ui.jobs import JobInfo, JobRunner
from app.ui.log_bridge import QtLogHandler
from app.ui.pages.base import Page
from app.ui.pages.dashboard import DashboardPage
from app.ui.pages.logs import LogsPage
from app.ui.pages.news import NewsPage
from app.ui.pages.scripts import ScriptsPage
from app.ui.pages.videos import VideosPage
from app.ui.widgets import Notifier, esc

HEALTH: dict[str, Callable[[AppContext], ServiceStatus]] = {
    "llm": health.check_llm, "ffmpeg": health.check_ffmpeg, "tts": health.check_tts,
    "wikimedia": health.check_wikimedia, "memory": lambda ctx: health.check_memory(),
}
RESULT_FR = {"READY": "vidéo prête", "NEEDS_REVIEW": "vidéo montée, relecture nécessaire", "BLOCKED": "sujet bloqué par le fact-check",
             "SCRIPT_BLOCKED": "script bloqué par les garde-fous", "FAILED": "échec", "CANCELLED": "annulé"}
AUTOPILOT_FR = {"PUBLISHED": "Publiée", "PENDING": "Envoyée (traitement YouTube)", "SKIPPED": "Écartée",
                "RETRY": "À renvoyer", "FAILED": "Erreur", "CANCELLED": "Annulé"}
PUBLISH_FR = {"PUBLISHED": "Publiée", "PENDING": "Envoyée (traitement en cours chez la plateforme)", "FAILED": "Échec"}


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, *, runner: JobRunner | None = None, notify: Notifier | None = None,
                 log_handler: QtLogHandler | None = None, health_fns: dict | None = None, player: bool = True,
                 pipeline_dialog=PipelineDialog, scheduler: Callable | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.scheduler_factory = scheduler or ctx.scheduler
        self.runner = runner or JobRunner(self)
        self.notify = notify or Notifier(self)
        self.health_fns = health_fns or HEALTH
        self.pipeline_dialog = pipeline_dialog
        self.last_html: tuple[str, str] | None = None
        self.setWindowTitle("Trump News")
        self.resize(1280, 820)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stack = QStackedWidget()
        self.pages: dict[str, Page] = {}
        for key, page in [("dashboard", DashboardPage(self)), ("news", NewsPage(self)), ("scripts", ScriptsPage(self)),
                          ("videos", VideosPage(self, player=player)), ("logs", LogsPage(self))]:
            self.pages[key] = page
            self.stack.addWidget(page)
            item = QListWidgetItem(page.title)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._on_nav)

        body = QWidget()
        lay = QHBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(body)

        self.job_label = QLabel("Aucune tâche en cours")
        self.job_bar = QProgressBar()
        self.job_bar.setFixedWidth(220)
        self.job_bar.setVisible(False)
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setToolTip("Arrête la chaîne entre deux étapes (une étape commencée va au bout).")
        self.cancel_btn.clicked.connect(self._cancel)
        sb = self.statusBar()
        sb.addWidget(self.job_label, 1)
        sb.addPermanentWidget(self.job_bar)
        sb.addPermanentWidget(self.cancel_btn)

        self.runner.started.connect(self._on_started)
        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        if log_handler is not None:
            log_handler.bridge.line.connect(self.pages["logs"].add_line)
        self.nav.setCurrentRow(0)

    # ------------------------------------------------------------ navigation
    def start(self) -> None:
        self.refresh_all()
        self.pages["dashboard"].check_services()
        self.refresh_schedule()

    def go(self, key: str, select=None) -> None:
        row = list(self.pages).index(key)
        self.nav.setCurrentRow(row)
        if select is not None:
            self.pages[key].select(select)

    def refresh_all(self) -> None:
        self.current_page().refresh()
        self._update_nav()

    def current_page(self) -> Page:
        return self.stack.currentWidget()

    def _on_nav(self, row: int) -> None:
        if row >= 0:
            self.stack.setCurrentIndex(row)
            self.stack.currentWidget().refresh()

    def _update_nav(self) -> None:
        n = self.ctx.reviews().pending_count()
        self.nav.item(list(self.pages).index("scripts")).setText(f"Scripts ({n} à relire)" if n else "Scripts")

    # ---------------------------------------------------------------- actions
    def health_check(self, key: str) -> ServiceStatus:          # appelé dans un fil de fond
        return self.health_fns[key](self.ctx)

    def fetch_news(self) -> None:
        def job(h):
            svc, providers = self.ctx.news_service()
            if not providers:
                from app.core.errors import TrumpNewsError
                raise TrumpNewsError("Aucun fournisseur d'actualités activé (news.providers).")
            return svc.run()
        self.runner.submit("Récupération des actualités", job, on_done=self._news_done,
                           on_error=lambda m: self.notify.error("Actualités", m))

    def _news_done(self, rep) -> None:
        failed = [r.error for r in rep.provider_status.values() if not r.ok]
        msg = (f"{rep.new_articles} nouvel(s) article(s) · {rep.stories_created} sujet(s) créé(s), "
               f"{rep.stories_updated} mis à jour")
        self.statusBar().showMessage(msg + (f" · {len(failed)} flux en échec (voir Journal)" if failed else ""), 15000)

    def run_fact_check(self, story_id: int) -> None:
        self.runner.submit(f"Fact-check du sujet #{story_id}",
                           lambda h: self.ctx.fact_checker().check(story_id),
                           on_done=lambda r: self.show_html(f"Fact-check du sujet #{story_id}", factcheck_html(r)),
                           on_error=lambda m: self.notify.error("Fact-check", m))

    def open_pipeline_dialog(self, story_id: int | None = None) -> None:
        svc, _ = self.ctx.news_service()
        dlg = self.pipeline_dialog(svc.list_stories(limit=40), story_id, self)
        if dlg.exec() and dlg.story_id() is not None:
            self.run_pipeline(dlg.story_id(), dlg.options())

    def run_pipeline(self, story_id: int, opts: PipelineOptions) -> JobInfo:
        return self.runner.submit(f"Pipeline du sujet #{story_id}",
                                  lambda h: PipelineRunner(self.ctx, progress=h.progress, token=h.token).run(story_id, opts),
                                  on_done=self._pipeline_done, on_error=lambda m: self.notify.error("Pipeline", m))

    def produce_video(self, script_id: int, story_id: int, preview: bool = False) -> JobInfo:
        opts = PipelineOptions(preview=preview)
        return self.runner.submit(f"Voix, visuels et montage du script #{script_id}",
                                  lambda h: PipelineRunner(self.ctx, progress=h.progress, token=h.token)
                                  .produce(script_id, story_id, opts),
                                  on_done=self._pipeline_done, on_error=lambda m: self.notify.error("Montage", m))

    def _pipeline_done(self, res: PipelineResult) -> None:
        steps = "".join(f"<li>{'✔' if s.ok else '✖'} {esc(s.name)} ({s.seconds:.0f} s) : {esc(s.detail)}</li>"
                        for s in res.steps)
        body = f"<p><b>{esc(RESULT_FR.get(res.status, res.status))}</b> — {esc(res.message)}</p><ul>{steps}</ul>"
        if res.status == "NEEDS_REVIEW" and res.script_id:
            body += (f"<p>Relisez le script #{res.script_id} (écran Scripts, bouton « Relire… ») pour rendre la vidéo "
                     "publiable.</p>")
        self.show_html(f"Pipeline du sujet #{res.story_id}", body)
        if res.video_id:
            self.go("videos", res.video_id)

    def run_autopilot(self) -> JobInfo:
        return self.runner.submit("Pilote automatique (génération et publication YouTube)",
                                  lambda h: self.ctx.autopilot(progress=h.progress, token=h.token).run(),
                                  on_done=self._autopilot_done,
                                  on_error=lambda m: self.notify.error("Pilote automatique", m))

    def refresh_schedule(self) -> None:
        self.runner.submit("État de la planification", lambda h: self.scheduler_factory().status(),
                           on_done=self.pages["dashboard"].set_schedule, exclusive=False,
                           on_error=lambda m: self.pages["dashboard"].schedule_label.setText(f"Planification : {esc(m)}"))

    def set_schedule(self, enable: bool) -> None:
        def job(h):
            svc = self.scheduler_factory()
            if enable:
                return svc.install()
            svc.remove()
            return svc.status()
        self.runner.submit("Activation de la planification" if enable else "Désactivation de la planification", job,
                           on_done=self.pages["dashboard"].set_schedule, exclusive=False,
                           on_error=lambda m: self.notify.error("Planification", m))

    def _autopilot_done(self, rep) -> None:
        rows = []
        for a in rep.attempts:
            link = f" · <a href='{esc(a.url)}'>{esc(a.url)}</a>" if a.url else ""
            rows.append(f"<li><b>{esc(AUTOPILOT_FR.get(a.status, a.status))}</b> sujet #{a.story_id} : "
                        f"{esc(a.title)}{link}<br>{esc(a.message)}</li>")
        self.show_html("Pilote automatique", f"<p><b>{esc(rep.message)}</b></p><ul>{''.join(rows)}</ul>")

    def render_video(self, script_id: int, *, template: str | None = None, preview: bool = False) -> JobInfo:
        return self.runner.submit(f"Montage du script #{script_id}",
                                  lambda h: self.ctx.video_service().render(script_id, template=template, preview=preview),
                                  on_done=lambda o: self.go("videos", o.video_id),
                                  on_error=lambda m: self.notify.error("Montage", m))

    def recheck_video(self, video_id: int) -> JobInfo:
        return self.runner.submit(f"Contrôle de la vidéo #{video_id}", lambda h: self.ctx.video_service().check(video_id),
                                  on_done=lambda o: self.go("videos", o.video_id),
                                  on_error=lambda m: self.notify.error("Contrôle qualité", m))

    def publish_video(self, video_id: int) -> JobInfo | None:
        """Publie la vidéo sur les plateformes activées, après contrôle des garde-fous et confirmation."""
        try:
            plan = self.ctx.publishing().prepare(video_id)
        except Exception as exc:                                 # vidéo introuvable, base inaccessible...
            self.notify.error("Publication", str(exc))
            return None
        if not plan.ok:
            self.notify.error("Publication impossible", "\n".join(f"- {p}" for p in plan.problems))
            return None
        if not plan.platforms:
            why = "\n".join(f"- {p} : {r}" for p, r in plan.skipped.items())
            self.notify.info("Publication", "Rien à envoyer.\n" + why)
            return None
        names = ", ".join(plan.platforms)
        if not self.notify.confirm("Publier la vidéo", f"Publier la vidéo #{video_id} sur : {names} ?\n\n"
                                                        f"Titre : {plan.request.title}\n"
                                                        "L'envoi est public et ne peut pas être annulé d'ici."):
            return None
        return self.runner.submit(f"Publication de la vidéo #{video_id} ({names})",
                                  lambda h: self.ctx.publishing().publish(video_id, plan.platforms)[1],
                                  on_done=lambda outcomes: self._publish_done(video_id, outcomes),
                                  on_error=lambda m: self.notify.error("Publication", m))

    def _publish_done(self, video_id: int, outcomes) -> None:
        rows = []
        for o in outcomes:
            link = f" · <a href='{esc(o.url)}'>{esc(o.url)}</a>" if o.url else ""
            detail = f"<br>{esc(o.detail)}" if o.detail else ""
            rows.append(f"<li><b>{esc(PUBLISH_FR.get(o.status, o.status))}</b> {esc(o.platform)}{link}{detail}</li>")
        self.show_html(f"Publication de la vidéo #{video_id}", f"<ul>{''.join(rows)}</ul>")

    def submit_review(self, script_id: int, *, approve: bool, note: str, sources_checked: bool) -> JobInfo:
        def job(h):
            svc = self.ctx.reviews()
            return (svc.approve(script_id, sources_checked=sources_checked, note=note) if approve
                    else svc.reject(script_id, note=note))

        def done(out) -> None:
            videos = ", ".join(f"vidéo #{v} : {s}" for v, s in out.video_statuses.items()) or "aucune vidéo à recontrôler"
            word = "approuvé" if out.decision == "APPROVED" else "refusé"
            self.statusBar().showMessage(f"Script #{script_id} {word} · {videos}", 20000)
            for w in out.warnings:
                self.notify.error("Relecture", w)

        return self.runner.submit(f"Relecture du script #{script_id}", job, on_done=done,
                                  on_error=lambda m: self.notify.error("Relecture", m))

    def show_html(self, title: str, html: str) -> None:
        self.last_html = (title, html)
        if not self.notify.quiet:
            HtmlDialog(title, html, self).exec()

    # ------------------------------------------------------------ barre de tâche
    def _on_started(self, info: JobInfo) -> None:
        if not info.exclusive:
            return
        waiting = len(self.runner.pending())
        self.job_label.setText(f"En cours : {info.title}" + (f"  (+{waiting} en attente)" if waiting else ""))
        self.job_bar.setRange(0, 0)
        self.job_bar.setVisible(True)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)

    def _on_progress(self, info: JobInfo, step: int, total: int, message: str) -> None:
        if not info.exclusive:
            return
        self.job_bar.setRange(0, total)
        self.job_bar.setValue(step)
        self.job_label.setText(f"En cours : {info.title} — {message}")

    def _on_finished(self, info: JobInfo, ok: bool, message: str) -> None:
        if info.exclusive and not self.runner.busy:
            self.job_label.setText(f"Terminé : {info.title}" if ok else f"Échec : {info.title}")
            self.job_bar.setVisible(False)
            self.cancel_btn.setVisible(False)
        if info.exclusive:
            self.refresh_all()

    def _cancel(self) -> None:
        self.runner.cancel_current()
        self.cancel_btn.setEnabled(False)
        self.job_label.setText(self.job_label.text() + " — annulation demandée (fin de l'étape en cours)")

    def closeEvent(self, event) -> None:
        if self.runner.busy and not self.notify.confirm(
                "Tâche en cours", "Une tâche est en cours. Quitter quand même ? Elle sera interrompue "
                                  "(l'étape en cours peut laisser un fichier incomplet, refait au prochain lancement)."):
            event.ignore()
            return
        self.runner.cancel_current()
        self.pages["videos"].player.stop()
        self.runner.wait(3000)
        event.accept()
