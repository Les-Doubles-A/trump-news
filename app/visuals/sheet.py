"""Planche-contact d'un plan visuel : un coup d'œil sur ce qui sera montré, segment par segment."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from app.visuals.imaging import load_font


def contact_sheet(items: list[dict], out: Path, cols: int = 5, thumb_w: int = 216) -> Path:
    thumb_h = int(thumb_w * 16 / 9)
    cap_h = 70
    rows = max(1, -(-len(items) // cols))
    sheet = Image.new("RGB", (cols * (thumb_w + 10) + 10, rows * (thumb_h + cap_h + 10) + 10), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    font = load_font(15, False)
    for i, it in enumerate(items):
        x, y = 10 + (i % cols) * (thumb_w + 10), 10 + (i // cols) * (thumb_h + cap_h + 10)
        try:
            with Image.open(it["asset"]["path"]) as im:
                im = im.convert("RGB")
                scale = max(thumb_w / im.width, thumb_h / im.height)
                im = im.resize((int(im.width * scale), int(im.height * scale)))
                left, top = (im.width - thumb_w) // 2, (im.height - thumb_h) // 2
                sheet.paste(im.crop((left, top, left + thumb_w, top + thumb_h)), (x, y))
        except Exception:
            d.rectangle([x, y, x + thumb_w, y + thumb_h], outline=(200, 80, 80))
        a = it["asset"]
        flag = " [IA]" if a.get("ai_generated") else ""
        d.text((x, y + thumb_h + 6), f"S{it['segment_index'] + 1}.{it['shot_index'] + 1}  {it['start']:.1f}-{it['end']:.1f}s", font=font, fill=(235, 235, 240))
        d.text((x, y + thumb_h + 28), f"{a['provider']}{flag} · {a.get('license') or ''}"[:34], font=font, fill=(160, 165, 180))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=88)
    return out
