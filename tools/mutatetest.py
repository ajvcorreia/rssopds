"""Mutate a known-good EPUB one variable at a time.

Three rounds of reasoning from our own output produced three wrong answers.
This works the other way round: start from a file the device *does* render
images from, change exactly one thing, and see what breaks.

The rewrite is byte-faithful. Entries are copied in their original order with
their original ZipInfo -- compression method, timestamps, external attributes
-- so the only difference from the source file is the thing being tested.

    python tools/mutatetest.py <reference.epub>

Produces:
    M1  reference with our image bytes substituted   -> tests our IMAGES
    M2  M1 plus our stylesheet's img rule            -> tests our CSS
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.config import config  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Category, Edition, EditionState  # noqa: E402

# The rule our books carry and the calibre file does not. A layout engine that
# mishandles "height: auto" can resolve it to zero and draw nothing.
OUR_IMG_CSS = "\nimg { max-width: 100%; height: auto; }\n"


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf"):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def our_image_bytes(label: str, fmt: str) -> bytes:
    """A picture encoded exactly the way this app's pipeline encodes them."""
    img = Image.new("RGB", (600, 380), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 599, 379], outline=(0, 0, 0), width=8)
    draw.rectangle([30, 30, 170, 350], fill=(190, 40, 40))
    font = _font(90)
    tw = draw.textlength(label, font=font)
    draw.text(((600 - tw) / 2 + 50, 140), label, font=font, fill=(0, 0, 0))
    buf = BytesIO()
    if fmt == "PNG":
        img.save(buf, "PNG", optimize=True)
    else:
        img.save(buf, "JPEG", quality=80, optimize=True, progressive=False)
    return buf.getvalue()


def mutate(source: Path, dest: Path, *, swap_images: bool,
           extra_css: str = "") -> Path:
    """Copy `source` entry for entry, changing only what is asked for."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as src, \
            zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info.filename)
            lower = info.filename.lower()

            if swap_images and lower.endswith((".jpg", ".jpeg", ".png")):
                fmt = "PNG" if lower.endswith(".png") else "JPEG"
                # Cover keeps a distinct label so it is identifiable too.
                label = "C" if "cover" in lower else "OK"
                data = our_image_bytes(label, fmt)
            elif extra_css and lower.endswith("stylesheet.css"):
                data = data + extra_css.encode("utf-8")

            # Reuse the original ZipInfo so ordering, compression method and
            # attributes are preserved exactly.
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            new_info.internal_attr = info.internal_attr
            new_info.create_system = info.create_system
            out.writestr(new_info, data)
    return dest


def _publish(name: str, slug: str, title: str, path: Path) -> str:
    with session_scope() as session:
        category = session.query(Category).filter_by(name=name).one_or_none()
        if category is None:
            category = Category(name=name, slug=slug, sort_order=0)
            session.add(category)
            session.flush()
        edition = (session.query(Edition).filter_by(category_id=category.id)
                   .order_by(Edition.id.desc()).first())
        if edition is None:
            edition = Edition(category_id=category.id, number=1, title=title)
            session.add(edition)
        edition.title = title
        edition.epub_file = path.name
        edition.size_bytes = path.stat().st_size
        edition.article_count = 1
        edition.state = EditionState.available
        edition.delivered_at = None
        edition.created_at = datetime.now()
        session.flush()
        return f"/opds/edition/{edition.id}/{path.stem}.epub"


def main(reference: Path) -> None:
    init_db()
    config.ensure_dirs()

    m1 = mutate(reference, config.epub_dir / "mutate-m1-ourimages.epub",
                swap_images=True)
    m2 = mutate(reference, config.epub_dir / "mutate-m2-ourcss.epub",
                swap_images=True, extra_css=OUR_IMG_CSS)

    print("published", _publish("Mut M1", "mut-m1",
                                "M1 - your file, our image bytes", m1))
    print("published", _publish("Mut M2", "mut-m2",
                                "M2 - M1 plus our img CSS rule", m2))
    for path in (m1, m2):
        print(f"  {path.name}  {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", help="an EPUB the reader renders images from")
    args = parser.parse_args()
    main(Path(args.reference))
