"""Category cover images.

A category can have an uploaded cover; otherwise one is generated. Generated
covers are deliberately high-contrast greyscale, because that is all an e-ink
catalogue grid can show, and they carry the edition date so two editions of the
same category are distinguishable on the device.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 800, 1200

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate(name: str, dest: Path, *, subtitle: str = "",
             count: int | None = None,
             width: int = WIDTH, height: int = HEIGHT) -> Path:
    """Render a cover for `name` into `dest`.

    Every dimension is expressed as a fraction of the canvas, so the same
    layout works whether it is drawn at 800x1200 or at 240x480 for a small
    e-ink panel -- a fixed 78px title would swallow a short screen whole.
    """
    # Deterministic shade per category, kept mid-range so white text reads.
    seed = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16)
    shade = 40 + (seed % 70)

    # RGB, not "L": a greyscale JPEG is single-component and some readers
    # cannot decode it. Looks identical on an e-ink screen.
    img = Image.new("RGB", (width, height), (shade, shade, shade))
    draw = ImageDraw.Draw(img)

    margin = round(width * 0.0875)
    band = min(shade + 28, 255)
    draw.rectangle([0, height * 0.317, width, height * 0.583],
                   fill=(band, band, band))
    rule = max(2, round(height * 0.0033))
    draw.rectangle([margin, height * 0.058, width - margin,
                    height * 0.058 + rule], fill=(235, 235, 235))
    draw.rectangle([margin, height - height * 0.062 - rule, width - margin,
                    height - height * 0.062], fill=(235, 235, 235))

    title_size = max(10, round(height * 0.065))
    floor = max(8, round(height * 0.033))
    title_font = _font(title_size)
    lines = _wrap(draw, name, title_font, width - margin * 2)
    while len(lines) > 3 and title_size > floor:
        title_size -= max(1, round(height * 0.0067))
        title_font = _font(title_size)
        lines = _wrap(draw, name, title_font, width - margin * 2)

    line_h = title_size + round(height * 0.013)
    y = height * 0.45 - (len(lines) * line_h) / 2
    for line in lines:
        w = draw.textlength(line, font=title_font)
        draw.text(((width - w) / 2, y), line, font=title_font,
                  fill=(245, 245, 245))
        y += line_h

    meta_font = _font(max(8, round(height * 0.032)))
    if subtitle:
        w = draw.textlength(subtitle, font=meta_font)
        draw.text(((width - w) / 2, height * 0.633), subtitle, font=meta_font,
                  fill=(225, 225, 225))
    if count is not None:
        label = f"{count} article{'s' if count != 1 else ''}"
        w = draw.textlength(label, font=meta_font)
        draw.text(((width - w) / 2, height * 0.679), label, font=meta_font,
                  fill=(210, 210, 210))

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=88, optimize=True, progressive=False)
    return dest


def resolve(category, cover_dir: Path, *, when: datetime, count: int = 0,
            width: int = WIDTH, height: int = HEIGHT) -> Path:
    """The cover file to serve for a category: uploaded, else generated.

    Shared by the OPDS catalogue and the web UI so the two can never drift --
    and so the web UI has a cover source that does not sit behind OPDS auth.
    """
    uploaded = (cover_dir / category.cover_file) if category.cover_file else None
    if uploaded and uploaded.exists():
        return uploaded
    return cover_for(category.name, None, cover_dir, when=when, count=count,
                     width=width, height=height)


def cover_for(category_name: str, uploaded: Path | None, scratch_dir: Path,
              *, when: datetime, count: int,
              width: int = WIDTH, height: int = HEIGHT) -> Path:
    """Uploaded cover if there is one, otherwise a freshly generated one."""
    if uploaded and uploaded.exists():
        return uploaded
    slug = hashlib.sha256(category_name.encode("utf-8")).hexdigest()[:12]
    # Size is part of the name so changing the screen setting regenerates
    # rather than serving a stale cover at the old dimensions.
    dest = scratch_dir / f"gen-{slug}-{width}x{height}-{when:%Y%m%d%H%M}.jpg"
    if not dest.exists():
        generate(category_name, dest, subtitle=f"{when:%A %d %B %Y}",
                 count=count, width=width, height=height)
    return dest
