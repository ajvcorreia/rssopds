"""Build several minimally-different EPUBs to find which packaging a reader likes.

The first diagnostic (tools/imagetest.py) varied the image: format, size,
colour mode, path shape, container element. All ten failed on a
CrossPoint-ESP32, yet that device renders images in other EPUBs. When every
image variant fails together, the image is not the variable -- the book is.

So these books each contain ONE image and differ only in how the package is
laid out. They are hand-rolled rather than built with ebooklib, because
ebooklib always emits an EPUB 3 with an "EPUB/" content folder, which is the
very thing under suspicion.

    python tools/packagingtest.py          # publish all of them
    python tools/packagingtest.py --out .  # write the files locally
"""
from __future__ import annotations

import argparse
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.config import config  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Category, Edition, EditionState  # noqa: E402

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF2 = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier>
    <dc:creator>Diagnostics</dc:creator>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch" href="{chapter}" media-type="application/xhtml+xml"/>
    <item id="pic" href="{image}" media-type="image/jpeg"/>
  </manifest>
  <spine toc="ncx"><itemref idref="ch"/></spine>
</package>"""

OPF3 = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">urn:uuid:{uid}</dc:identifier>
    <dc:creator>Diagnostics</dc:creator>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch" href="{chapter}" media-type="application/xhtml+xml"/>
    <item id="pic" href="{image}" media-type="image/jpeg"/>
  </manifest>
  <spine toc="ncx"><itemref idref="ch"/></spine>
</package>"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:{uid}"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap><navPoint id="n1" playOrder="1">
    <navLabel><text>{title}</text></navLabel>
    <content src="{chapter}"/>
  </navPoint></navMap>
</ncx>"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol><li><a href="{chapter}">{title}</a></li></ol></nav></body>
</html>"""

CHAPTER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<p>{blurb}</p>
<p><img src="{src}" alt="test"/></p>
<p>If you can see a picture above this line, layout <b>{letter}</b> works.</p>
</body>
</html>"""


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf"):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def picture(letter: str, plain: bool) -> bytes:
    """A big letter. `plain` writes a bog-standard web-style JPEG."""
    img = Image.new("RGB", (400, 260), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 399, 259], outline=(0, 0, 0), width=6)
    font = _font(150)
    tw = draw.textlength(letter, font=font)
    draw.text(((400 - tw) / 2, 40), letter, font=font, fill=(0, 0, 0))
    buf = BytesIO()
    if plain:
        # No greyscale conversion, no optimised Huffman tables -- as close to
        # what a camera or a website would produce as possible.
        img.save(buf, "JPEG", quality=85, optimize=False, progressive=False,
                 subsampling=2)
    else:
        img.convert("L").save(buf, "JPEG", quality=85, optimize=True,
                              progressive=False)
    return buf.getvalue()


def write_epub(path: Path, *, title: str, root: str, image_rel: str,
               src: str, version: str, letter: str, blurb: str,
               plain_jpeg: bool = False) -> Path:
    """Assemble an EPUB by hand so the layout is exactly as specified."""
    uid = uuid.uuid4()
    prefix = f"{root}/" if root else ""
    chapter_name = "chapter.xhtml"

    opf_tpl = OPF2 if version == "2.0" else OPF3
    opf = opf_tpl.format(title=title, uid=uid, chapter=chapter_name,
                         image=image_rel,
                         modified=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    chapter = CHAPTER.format(title=title, src=src, letter=letter, blurb=blurb)

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype must be first and stored uncompressed.
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   CONTAINER.format(opf=f"{prefix}content.opf"))
        z.writestr(f"{prefix}content.opf", opf)
        z.writestr(f"{prefix}toc.ncx",
                   NCX.format(uid=uid, title=title, chapter=chapter_name))
        if version == "3.0":
            z.writestr(f"{prefix}nav.xhtml",
                       NAV.format(chapter=chapter_name, title=title))
        z.writestr(f"{prefix}{chapter_name}", chapter)
        z.writestr(f"{prefix}{image_rel}", picture(letter, plain_jpeg))
    return path


LAYOUTS = [
    dict(letter="A", slug="pkg-a-flat", title="A - flat, no folder",
         root="", image_rel="pic.jpg", src="pic.jpg", version="2.0",
         blurb="Everything at the zip root, EPUB 2, image beside the chapter."),
    dict(letter="B", slug="pkg-b-oebps", title="B - OEBPS, EPUB 2",
         root="OEBPS", image_rel="images/pic.jpg", src="images/pic.jpg",
         version="2.0",
         blurb="Classic OEBPS folder, EPUB 2, image in an images/ subfolder."),
    dict(letter="C", slug="pkg-c-rootpath", title="C - EPUB3, path from zip root",
         root="EPUB", image_rel="images/pic.jpg", src="EPUB/images/pic.jpg",
         version="3.0",
         blurb="Current layout, but the img src is written from the zip root."),
    dict(letter="D", slug="pkg-d-plainjpeg", title="D - EPUB3, plain colour JPEG",
         root="EPUB", image_rel="images/pic.jpg", src="images/pic.jpg",
         version="3.0", plain_jpeg=True,
         blurb="Current layout, but an ordinary colour JPEG with standard "
               "Huffman tables instead of an optimised greyscale one."),
]


def publish() -> list[str]:
    init_db()
    urls = []
    for i, spec in enumerate(LAYOUTS):
        spec = dict(spec)
        slug = spec.pop("slug")
        filename = f"{slug}.epub"
        path = write_epub(config.epub_dir / filename, **spec)

        with session_scope() as session:
            name = f"Pkg {spec['letter']}"
            category = session.query(Category).filter_by(name=name).one_or_none()
            if category is None:
                category = Category(name=name, slug=slug, sort_order=i)
                session.add(category)
                session.flush()
            edition = (session.query(Edition).filter_by(category_id=category.id)
                       .order_by(Edition.id.desc()).first())
            if edition is None:
                edition = Edition(category_id=category.id, number=1,
                                  title=spec["title"])
                session.add(edition)
            edition.title = spec["title"]
            edition.epub_file = filename
            edition.size_bytes = path.stat().st_size
            edition.article_count = 1
            edition.state = EditionState.available
            edition.delivered_at = None
            edition.created_at = datetime.now()
            session.flush()
            urls.append(f"/opds/edition/{edition.id}/{slug}.epub")
    return urls


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="write the epubs into this directory")
    args = parser.parse_args()
    if args.out:
        for spec in LAYOUTS:
            spec = dict(spec)
            slug = spec.pop("slug")
            print("wrote", write_epub(Path(args.out) / f"{slug}.epub", **spec))
    else:
        for url in publish():
            print("published", url)
