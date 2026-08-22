"""Thumbnail: one strong NASA image, hard vignette, heavy title."""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .nasa import Asset
from .util import ROOT, config, log

LOGGER = log("thumb")
W, H = 1280, 720


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _pick_image(assets: list[Asset]) -> Asset | None:
    """Prefer a wide, downloaded still with a real description."""
    stills = [a for a in assets if a.media_type == "image" and a.local and Path(a.local).exists()]
    if not stills:
        return None

    def score(a: Asset) -> float:
        s = 0.0
        try:
            with Image.open(a.local) as im:
                w, h = im.size
            if w >= 1280:
                s += 2
            if 1.2 <= w / max(1, h) <= 2.2:   # avoid extreme panoramas and portraits
                s += 2
        except Exception:  # noqa: BLE001
            return -1
        s += min(2.0, len(a.description) / 400)
        return s

    ranked = sorted(stills, key=score, reverse=True)
    return ranked[0] if score(ranked[0]) > 0 else None


def build(title: str, assets: list[Asset], dest: Path) -> Path | None:
    b = config()["brand"]
    navy, silver_hi, steel = _hex(b["navy"]), _hex(b["silver_hi"]), _hex(b["steel"])

    asset = _pick_image(assets)
    if not asset:
        LOGGER.warning("no suitable still for thumbnail")
        return None

    with Image.open(asset.local) as raw:
        base = raw.convert("RGB")

    # cover-crop to 16:9
    scale = max(W / base.width, H / base.height)
    base = base.resize((int(base.width * scale), int(base.height * scale)), Image.LANCZOS)
    left = (base.width - W) // 2
    top = int((base.height - H) * 0.42)
    img = base.crop((left, top, left + W, top + H))

    img = ImageEnhance.Contrast(img).enhance(1.22)
    img = ImageEnhance.Color(img).enhance(0.88)

    # navy grade + left-weighted scrim so the title always reads
    grade = Image.new("RGB", (W, H), navy)
    img = Image.blend(img, grade, 0.24)

    scrim = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(scrim)
    for x in range(W):
        t = max(0.0, 1 - (x / (W * 0.72)))
        sd.line([(x, 0), (x, H)], fill=int(215 * (t ** 1.25)))
    scrim = scrim.filter(ImageFilter.GaussianBlur(24))
    img = Image.composite(Image.new("RGB", (W, H), navy), img, scrim)

    d = ImageDraw.Draw(img)

    words = title.upper().split()
    lines = textwrap.wrap(" ".join(words), width=15)[:3]

    size = 96
    while size > 44:
        f = ImageFont.truetype(str(ROOT / b["font_title"]), size)
        if max(d.textlength(l, font=f) for l in lines) <= W * 0.60:
            break
        size -= 3
    font = ImageFont.truetype(str(ROOT / b["font_title"]), size)
    lh = int(size * 1.08)

    x0 = 64
    y0 = (H - lh * len(lines)) // 2 + 18

    d.line([(x0, y0 - 46), (x0 + 108, y0 - 46)], fill=steel, width=7)

    for i, line in enumerate(lines):
        y = y0 + i * lh
        for ox, oy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
            d.text((x0 + ox, y + oy), line, font=font, fill=navy)
        d.text((x0, y), line, font=font, fill=silver_hi)

    kicker = ImageFont.truetype(str(ROOT / b["font_medium"]), 30)
    d.text((x0, y0 - 96), "NASA ARCHIVE", font=kicker, fill=steel)

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=90, optimize=True)

    kb = dest.stat().st_size / 1024
    if kb > 2000:  # YouTube hard limit
        img.save(dest, "JPEG", quality=78, optimize=True)

    LOGGER.info("thumbnail: %s (%.0f KB, source %s)", dest.name, kb, asset.nasa_id)
    return dest
