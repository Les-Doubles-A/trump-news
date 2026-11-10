"""Mise en forme HTML (QTextBrowser) des résultats : fact-check, script à relire, rapport de contrôle qualité."""
from __future__ import annotations

from app.review.service import ScriptDetail
from app.ui.widgets import badge_html, color_for, esc, local_time

SECTION_FR = {"hook": "Accroche", "context": "Contexte", "main": "Principal", "details": "Détails", "impact": "Impact",
              "conclusion": "Conclusion"}
TYPE_FR = {"fact": "fait", "attributed": "attribué", "disputed": "divergence", "opinion": "opinion", "context": "liaison"}


def factcheck_html(r) -> str:
    """r : FactCheckResult."""
    levels = {lv: sum(1 for x in r.sources if x["level"] == lv) for lv in ("full", "summary", "title")}
    out = [f"<h3>Sujet #{r.story_id} — {esc(r.story_title)}</h3>",
           f"<p>Verdict : {badge_html(r.verdict)} (confiance {r.confidence:.2f}) · modèle {esc(r.model or 'auto')}"
           f"{' · résultat en cache' if r.from_cache else ''}<br>",
           f"Sources : {len(r.sources)} ({levels['full']} texte complet, {levels['summary']} résumé, "
           f"{levels['title']} titre seul) · affirmations : {r.llm_claims} proposée(s), {len(r.claims)} retenue(s)</p>"]
    for c in r.claims:
        out.append(f"<p>{badge_html(c.status)} <i>({esc(c.kind)})</i> <b>{esc(c.claim)}</b><br>")
        for e in c.supporting:
            out.append(f"&nbsp;&nbsp;« {esc(e.quote)} » — <a href='{esc(e.url)}'>{esc(e.source_name)}</a><br>")
        for e in c.contradicting:
            out.append(f"&nbsp;&nbsp;<span style='color:{color_for('DISPUTED')}'>contredit par</span> « {esc(e.quote)} » — "
                       f"{esc(e.source_name)}<br>")
        out.append(f"&nbsp;&nbsp;<i>{esc(c.phrasing)}</i></p>")
    if r.dropped:
        out.append("<h4>Affirmations écartées</h4><ul>" +
                   "".join(f"<li>{esc(d['reason'])} : {esc(d['claim'])}</li>" for d in r.dropped) + "</ul>")
    if r.warnings:
        out.append("<h4>Avertissements</h4><ul>" + "".join(f"<li>{esc(w)}</li>" for w in r.warnings) + "</ul>")
    return "".join(out)


def script_html(d: ScriptDetail) -> str:
    out = [f"<h3>Script #{d.id} · sujet #{d.story_id}</h3>",
           f"<p>Statut : {badge_html(d.status)} · fact-check : {badge_html(d.verdict)} · {esc(d.style)} · "
           f"{esc(d.language)} · cible {d.duration_target} s</p>"]
    for seg in d.segments:
        out.append(f"<p><b>{esc(SECTION_FR.get(seg['section'], seg['section']))}</b> "
                   f"<i>({esc(TYPE_FR.get(seg['type'], seg['type']))})</i><br>"
                   f"<span style='font-size:15px'>{esc(seg['text'])}</span><br>")
        for cid in seg.get("claims", []):
            c = d.claims.get(cid)
            if not c:
                out.append(f"&nbsp;&nbsp;<span style='color:{color_for('FAILED')}'>{esc(cid)} : affirmation introuvable</span><br>")
                continue
            out.append(f"&nbsp;&nbsp;{esc(cid)} {badge_html(c['status'])} <i>{esc(c['claim'])}</i><br>")
            for src in c.get("sources", []):
                link = f"<a href='{esc(src.get('url'))}'>{esc(src.get('name'))}</a>" if src.get("url") else esc(src.get("name"))
                quote = f"« {esc(src['quote'])} » — " if src.get("quote") else ""
                out.append(f"&nbsp;&nbsp;&nbsp;&nbsp;{quote}{link}<br>")
        out.append("</p>")
    if d.warnings:
        out.append("<h4>Avertissements</h4><ul>" + "".join(f"<li>{esc(w)}</li>" for w in d.warnings) + "</ul>")
    if d.reviews:
        out.append("<h4>Relectures</h4><ul>" + "".join(
            f"<li>{badge_html(r['decision'])} par {esc(r['reviewer'])} le {local_time(r['at'], '%d/%m/%Y %H:%M')}"
            f"{' — ' + esc(r['note']) if r.get('note') else ''}</li>" for r in d.reviews) + "</ul>")
    return "".join(out)


def quality_html(report: dict | None) -> str:
    if not report:
        return "<p><i>Aucun rapport pour cette vidéo.</i></p>"
    if "error" in report:
        return f"<p>{badge_html('FAILED')} {esc(report['error'])}</p>"
    q = report.get("quality", {})
    out = [f"<p>Statut : {badge_html(q.get('status'))} · score qualité {q.get('score', 0):.2f} · template "
           f"{esc(report.get('template'))}{' · aperçu' if report.get('preview') else ''}</p><table cellspacing='4'>"]
    for c in q.get("checks", []):
        key = "READY" if c["ok"] else ("FAILED" if c["critical"] else "NEEDS_REVIEW")
        mark = "OK" if c["ok"] else ("bloquant" if c["critical"] else "pénalité")
        out.append(f"<tr><td style='color:{color_for(key)}; font-weight:600'>{mark}</td><td>{esc(c['name'])}</td>"
                   f"<td>{esc(c['detail'])}</td></tr>")
    out.append("</table>")
    if report.get("credits"):
        out.append("<h4>Crédits (pour la description)</h4><ul>" +
                   "".join(f"<li>{esc(c)}</li>" for c in report["credits"]) + "</ul>")
    if report.get("warnings"):
        out.append("<h4>Avertissements</h4><ul>" + "".join(f"<li>{esc(w)}</li>" for w in report["warnings"]) + "</ul>")
    return "".join(out)
