"""Outils d'image (Pillow) : sondage, JPEG, hash, badge « IA », polices, découpage du texte."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from app.core.errors import ProviderError

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_REGULAR_CANDIDATES = [c.replace("segoeuib", "segoeui").replace("arialbd", "arial").replace("calibrib", "calibri")
                       .replace("DejaVuSans-Bold", "DejaVuSans").replace("LiberationSans-Bold", "LiberationSans-Regular")
                       .replace("Arial Bold", "Arial") for c in _FONT_CANDIDATES]


def load_font(size: int, bold: bool = True, font_path: str | None = None) -> ImageFont.ImageFont:
    for cand in ([font_path] if font_path else []) + (_FONT_CANDIDATES if bold else _REGULAR_CANDIDATES):
        if cand and Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            return im.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ProviderError(f"Image illisible : {path.name}") from exc


def save_jpeg(data: bytes | Path, dest: Path, max_side: int = 2400) -> tuple[int, int]:
    """Décode (ou lit), convertit en RGB, borne la taille et enregistre en JPEG. Lève ProviderError si illisible."""
    import io

    try:
        im = Image.open(io.BytesIO(data) if isinstance(data, bytes) else data)
        im.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ProviderError("Image téléchargée illisible ou corrompue.") from exc
    im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=90)
    return im.size


def add_badge(path: Path, text: str) -> None:
    """Incruste un bandeau (ex. « AI-generated illustration ») en bas de l'image, de façon indélébile."""
    with Image.open(path) as im:
        im = im.convert("RGB")
    w, h = im.size
    font = load_font(max(18, w // 30))
    draw = ImageDraw.Draw(im, "RGBA")
    tw = draw.textlength(text, font=font)
    pad = max(8, w // 70)
    box_h = font.size + pad * 2
    draw.rectangle([0, h - box_h, w, h], fill=(0, 0, 0, 190))
    draw.text(((w - tw) / 2, h - box_h + pad - 2), text, font=font, fill=(255, 255, 255, 255))
    im.save(path, "PNG" if path.suffix.lower() == ".png" else "JPEG")


def wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int, start: int, minimum: int = 36,
             font_path: str | None = None, bold: bool = True) -> tuple[ImageFont.ImageFont, list[str]]:
    """Plus grande taille de police dont le texte tient dans (max_w, max_h)."""
    size = start
    while size >= minimum:
        font = load_font(size, bold, font_path)
        lines = wrap(draw, text, font, max_w)
        if len(lines) * int(size * 1.25) <= max_h:
            return font, lines
        size -= 4
    font = load_font(minimum, bold, font_path)
    return font, wrap(draw, text, font, max_w)
