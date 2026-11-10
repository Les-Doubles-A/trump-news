"""Cartes graphiques 1080x1920 générées localement (aucun droit à gérer, aucune dépendance réseau)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PIL import Image, ImageDraw

from app.visuals.imaging import fit_text, load_font

W, H = 1080, 1920
THEMES = {   # fond haut, fond bas, accent
    "politics": ((18, 28, 56), (8, 12, 28), (255, 196, 61)),
    "white_house": ((22, 36, 70), (9, 14, 32), (230, 230, 240)),
    "elections": ((52, 20, 36), (20, 8, 16), (255, 120, 120)),
    "justice": ((34, 30, 22), (14, 12, 8), (222, 184, 120)),
    "economy": ((14, 46, 40), (6, 18, 16), (110, 231, 183)),
    "international": ((20, 38, 62), (8, 16, 28), (120, 190, 255)),
    "controversy": ((56, 24, 20), (22, 10, 8), (255, 150, 90)),
    "statements": ((30, 30, 52), (12, 12, 24), (190, 170, 255)),
}


def _gradient(top, bottom) -> Image.Image:
    im = Image.new("RGB", (W, H), top)
    px = ImageDraw.Draw(im)
    for y in range(H):
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return im


def card_id(kind: str, text: str, footer: str, badge: str, category: str) -> str:
    return hashlib.sha256("|".join([kind, text, footer, badge, category]).encode()).hexdigest()[:16]


def render_card(kind: str, text: str, out: Path, *, footer: str = "", badge: str = "", category: str = "politics",
                font_path: str | None = None) -> Path:
    top, bottom, accent = THEMES.get(category, THEMES["politics"])
    im = _gradient(top, bottom)
    d = ImageDraw.Draw(im)
    margin = 90
    d.rectangle([margin, 300, margin + 140, 312], fill=accent)                    # filet d'accent

    if kind == "number":
        m = re.search(r"[$€£]?\s?\d[\d.,]*\s?(?:%|percent|million|billion|milliard\w*|millón|millones)?", text)
        big = (m.group(0).strip() if m else text)[:18]
        font, lines = fit_text(d, big, W - 2 * margin, 420, 260, 90, font_path)
        y = 520
        for line in lines:
            d.text((margin, y), line, font=font, fill=accent)
            y += int(font.size * 1.2)
        rest = text
        f2, l2 = fit_text(d, rest, W - 2 * margin, 560, 72, 40, font_path, bold=False)
        y += 40
        for line in l2:
            d.text((margin, y), line, font=f2, fill=(240, 240, 245))
            y += int(f2.size * 1.3)
    else:
        start = 120 if kind == "headline" else 84
        font, lines = fit_text(d, text, W - 2 * margin, 1000, start, 44, font_path)
        y = 400
        for line in lines:
            d.text((margin, y), line, font=font, fill=(250, 250, 252))
            y += int(font.size * 1.25)

    if badge:
        bf = load_font(46, True, font_path)
        tw = d.textlength(badge.upper(), font=bf)
        d.rounded_rectangle([margin, 150, margin + tw + 60, 150 + 84], radius=14, fill=accent)
        d.text((margin + 30, 150 + 16), badge.upper(), font=bf, fill=(15, 15, 20))
    if footer:
        ff, fl = fit_text(d, footer, W - 2 * margin, 220, 52, 32, font_path, bold=False)
        y = H - 200 - len(fl) * int(ff.size * 1.3)
        for line in fl:
            d.text((margin, y), line, font=ff, fill=(185, 190, 205))
            y += int(ff.size * 1.3)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG")
    return out
