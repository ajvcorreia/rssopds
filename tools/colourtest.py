"""A/B test greyscale vs colour images, inside the real EPUB writer.

The first image diagnostic varied colour mode, but it did so inside an
ebooklib-shaped book that the target reader would not read images from at all,
so the result was meaningless. Now that the package layout is known-good
(copied from a calibre book the device does render), colour mode is the
remaining untested difference: every image this app produced was a
single-component greyscale JPEG, while all 71 images in the working calibre
book are RGB. Plenty of small JPEG decoders only implement 3-component YCbCr.

This builds one book through the real pipeline.epub writer, with three
articles whose only difference is how their picture is encoded.

    python tools/colourtest.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.config import config  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Category, Edition, EditionState  # noqa: E402
from app.pipeline import epub  # noqa: E402


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf"):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def card(letter: str) -> Image.Image:
    img = Image.new("RGB", (600, 380), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 599, 379], outline=(0, 0, 0), width=8)
    draw.rectangle([40, 40, 200, 340], fill=(190, 40, 40))
    font = _font(220)
    tw = draw.textlength(letter, font=font)
    draw.text(((600 - tw) / 2 + 60, 60), letter, font=font, fill=(0, 0, 0))
    return img


class _Feed:
    title = "Colour test"


class _Article:
    byline = None
    feed = _Feed()
    published_at = None
    word_count = 20
    part_count = 1
    body_html = "<p>If a picture appears above, this encoding works.</p>"

    def __init__(self, title: str, image_file: str):
        self.title = title
        self.image_file = image_file


VARIANTS = [
    ("G", "grey.jpg", "1 - GREYSCALE JPEG (what this app has always made)",
     lambda im, p: im.convert("L").save(p, "JPEG", quality=85, optimize=True,
                                        progressive=False)),
    ("C", "colour.jpg", "2 - COLOUR JPEG (what calibre makes)",
     lambda im, p: im.convert("RGB").save(p, "JPEG", quality=85,
                                          optimize=True, progressive=False)),
    ("P", "colour.png", "3 - COLOUR PNG",
     lambda im, p: im.convert("RGB").save(p, "PNG", optimize=True)),
]


def publish() -> str:
    init_db()
    config.ensure_dirs()

    articles = []
    for letter, filename, title, save in VARIANTS:
        path = config.image_dir / f"colourtest-{filename}"
        save(card(letter), path)
        articles.append(_Article(title, path.name))

    filename = "colour-test.epub"
    out = config.epub_dir / filename
    epub.build(title="Colour test", author="Diagnostics", articles=articles,
               cover_path=None, image_dir=config.image_dir, out_path=out,
               when=datetime.now())

    with session_scope() as session:
        name = "Colour Test"
        category = session.query(Category).filter_by(name=name).one_or_none()
        if category is None:
            category = Category(name=name, slug="colour-test", sort_order=0)
            session.add(category)
            session.flush()
        edition = (session.query(Edition).filter_by(category_id=category.id)
                   .order_by(Edition.id.desc()).first())
        if edition is None:
            edition = Edition(category_id=category.id, number=1,
                              title="Colour test")
            session.add(edition)
        edition.title = "Colour test"
        edition.epub_file = filename
        edition.size_bytes = out.stat().st_size
        edition.article_count = len(articles)
        edition.state = EditionState.available
        edition.delivered_at = None
        edition.created_at = datetime.now()
        session.flush()
        return f"/opds/edition/{edition.id}/colour-test.epub"


if __name__ == "__main__":
    print("published", publish())
