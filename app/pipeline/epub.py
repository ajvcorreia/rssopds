"""Build an EPUB from a category's unread articles.

Deliberately hand-rolled rather than built with ebooklib, and deliberately
EPUB 2 rather than 3.

The reason is empirical. A CrossPoint-ESP32 reader rendered the text of an
ebooklib-produced book but none of its images, across ten variants covering
format, pixel size, colour mode, path shape and container element -- while
happily rendering images from a calibre-produced book. Comparing the two, the
calibre file ships *progressive* JPEGs at 2560x1440 and 1.2MB, so encoding,
dimensions and colour mode were never the problem. What differed was the
package:

    calibre                          ebooklib
    -------                          --------
    <package version="2.0">          <package version="3.0">
    content.opf at the zip root      EPUB/content.opf
    toc.ncx only                     nav.xhtml + toc.ncx
    article dirs beside the opf      everything under EPUB/
    short .html names                long .xhtml names

So this writer reproduces the layout that is known to work on the target
device. It is also the more conservative choice in general: EPUB 2 with an NCX
is understood by everything, including readers far older than EPUB 3.

Layout produced:

    mimetype                      (stored, first entry)
    META-INF/container.xml
    content.opf                   at the root, as calibre does
    toc.ncx
    stylesheet.css
    cover.jpg
    index.html                    contents page
    article_000/index.html
    article_000/images/img1.jpg
"""
from __future__ import annotations

import html as html_lib
import logging
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from ..models import Article
from . import clean

log = logging.getLogger(__name__)

CSS = """
body { font-family: serif; line-height: 1.45; margin: 0 6%; }
h1 { font-size: 1.35em; line-height: 1.25; margin: 1em 0 0.2em; }
h2 { font-size: 1.1em; margin: 1.2em 0 0.3em; }
p { margin: 0 0 0.75em; text-align: justify; }
img { max-width: 100%; height: auto; }
figure { margin: 1em 0; text-align: center; }
figcaption { font-size: 0.8em; font-style: italic; color: #444; }
blockquote { margin: 0.8em 1.2em; font-style: italic; }
pre { font-size: 0.8em; white-space: pre-wrap; word-wrap: break-word; }
.meta { font-size: 0.78em; color: #555; margin: 0 0 1.2em;
        border-bottom: 1px solid #bbb; padding-bottom: 0.5em; }
.thread-note { font-size: 0.78em; color: #555; font-style: italic; }
.hero { margin: 0 0 1.2em; text-align: center; }
table { border-collapse: collapse; width: 100%; font-size: 0.85em; }
td, th { border: 1px solid #999; padding: 0.3em; }
h2.feed { font-size: 1em; margin: 1.4em 0 0.3em; padding-bottom: 0.2em;
          border-bottom: 1px solid #bbb; }
ul.toc { list-style: none; padding-left: 0; margin: 0 0 1em; }
ul.toc li { margin: 0 0 0.6em; }
ul.toc a { text-decoration: underline; }
"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

# An XML declaration and no DOCTYPE, exactly as calibre writes them.
PAGE = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>{title}</title>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<link rel="stylesheet" type="text/css" href="{css}"/>
</head>
<body>{body}</body>
</html>
"""


def _esc(text: str) -> str:
    return html_lib.escape(text or "", quote=False)


def _attr(text: str) -> str:
    return html_lib.escape(text or "", quote=True)


def _referenced_images(html: str) -> list[str]:
    """Stored image filenames an article's body points at, in order."""
    soup = BeautifulSoup(html or "", "lxml")
    out: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src.startswith("images/"):
            name = src.split("/", 1)[1]
            if name not in out:
                out.append(name)
    return out


def _plan_images(article: Article, embed_images: bool = True) -> dict[str, str]:
    """Map stored filename -> short in-book name (img1.jpg, img2.jpg, ...).

    Short per-article names mirror calibre and keep paths well clear of any
    fixed-size path buffer a small device might have.
    """
    if not embed_images:
        return {}
    names: list[str] = []
    if article.image_file:
        names.append(article.image_file)
    for name in _referenced_images(article.body_html):
        if name not in names:
            names.append(name)
    return {stored: f"img{i}{Path(stored).suffix or '.jpg'}"
            for i, stored in enumerate(names, start=1)}


def _chapter_html(article: Article, index: int = 0, embed_images: bool = True,
                  mapping: dict[str, str] | None = None) -> str:
    """The body markup for one article."""
    if mapping is None:
        mapping = _plan_images(article, embed_images)

    bits = [f"<h1>{_esc(article.title)}</h1>"]

    meta: list[str] = []
    if article.byline:
        meta.append(_esc(article.byline))
    if article.feed and article.feed.title:
        meta.append(_esc(article.feed.title))
    if article.published_at:
        meta.append(article.published_at.strftime("%d %b %Y %H:%M"))
    if article.word_count:
        meta.append(f"{article.word_count} words")
    if meta:
        bits.append(f'<p class="meta">{" &#183; ".join(meta)}</p>')

    if article.part_count > 1:
        bits.append(f'<p class="thread-note">Thread of {article.part_count} '
                    f"posts, joined into one piece.</p>")

    body = article.body_html or "<p><em>No content.</em></p>"
    if not embed_images:
        body = clean.strip_images(body)
    else:
        # Rewrite the stored hash filenames to the short per-article ones.
        for stored, short in mapping.items():
            body = body.replace(f'src="images/{stored}"',
                                f'src="images/{short}"')

    # The hero lives on the article rather than in the body, so place it here
    # or it never reaches the book at all.
    hero = article.image_file if embed_images else None
    if hero and hero in mapping and f"images/{mapping[hero]}" not in body:
        bits.append(f'<div class="hero"><img src="images/{mapping[hero]}" '
                    f'alt=""/></div>')

    bits.append(body)
    return "".join(bits)


def _feed_name(article: Article) -> str:
    return (article.feed.title if article.feed and article.feed.title
            else "Other")


def group_by_feed(articles: list[Article]) -> list[tuple[str, list[Article]]]:
    """Articles bucketed under their feed, newest first within each.

    Feeds keep the order in which they first appear, so the sequence is stable
    between editions rather than jumping about alphabetically.
    """
    order: list[str] = []
    groups: dict[str, list[Article]] = {}
    for article in articles:
        name = _feed_name(article)
        if name not in groups:
            order.append(name)
            groups[name] = []
        groups[name].append(article)
    return [(name, groups[name]) for name in order]


def _contents_html(articles: list[Article]) -> str:
    """Contents grouped under a heading per feed.

    The markup deliberately mirrors what calibre emits -- a plain <ul> whose
    <li> holds nothing but the anchor. Anything else inside the <li> is a
    chance for a simple reader to mis-handle the link.
    """
    index = 0
    blocks = ["<h1>Contents</h1>"]
    for slot, (feed_name, group) in enumerate(group_by_feed(articles)):
        blocks.append(f'<h2 class="feed" id="feed_{slot}">{_esc(feed_name)}</h2>')
        blocks.append('<ul class="toc">')
        for article in group:
            blocks.append(
                f'<li><a href="article_{index:03d}/index.html">'
                f"{_esc(article.title)}</a></li>")
            index += 1
        blocks.append("</ul>")
    return "".join(blocks)


def _opf(title: str, author: str, uid: str, when: datetime,
         articles: list[Article], image_entries: list[tuple[str, str]],
         has_cover: bool) -> str:
    manifest = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="stylesheet.css" media-type="text/css"/>',
        '<item id="contents" href="index.html" '
        'media-type="application/xhtml+xml"/>',
    ]
    if has_cover:
        manifest.append('<item id="cover" href="cover.jpg" '
                        'media-type="image/jpeg"/>')
    for index in range(len(articles)):
        manifest.append(
            f'<item id="art{index}" href="article_{index:03d}/index.html" '
            f'media-type="application/xhtml+xml"/>')
    for i, (href, media) in enumerate(image_entries):
        manifest.append(f'<item id="img{i}" href="{_attr(href)}" '
                        f'media-type="{media}"/>')

    spine = ['<itemref idref="contents"/>']
    spine += [f'<itemref idref="art{i}"/>' for i in range(len(articles))]

    cover_meta = '<meta name="cover" content="cover"/>' if has_cover else ""
    guide = ('<guide>'
             '<reference type="toc" title="Contents" href="index.html"/>'
             + ('<reference type="cover" title="Cover" href="cover.jpg"/>'
                if has_cover else "")
             + "</guide>")

    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
        'unique-identifier="uuid_id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f"    <dc:title>{_esc(title)}</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        f'    <dc:identifier id="uuid_id" opf:scheme="uuid">{uid}'
        "</dc:identifier>\n"
        f"    <dc:date>{when.strftime('%Y-%m-%d')}</dc:date>\n"
        + (f'    <dc:creator opf:role="aut">{_esc(author)}</dc:creator>\n'
           if author else "")
        + f"    {cover_meta}\n"
        "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(manifest) + "\n  </manifest>\n"
        '  <spine toc="ncx">\n    ' + "\n    ".join(spine) + "\n  </spine>\n"
        f"  {guide}\n"
        "</package>\n"
    )


def _ncx(title: str, uid: str, articles: list[Article]) -> str:
    """Two-level navigation: each feed, with its articles nested beneath.

    The reader's own contents menu is built from this, so it gets the same
    grouping as the contents page -- which matters on a device where that menu
    is the navigation that reliably works.
    """
    points = ['<navPoint id="nav-toc" playOrder="1">'
              "<navLabel><text>Contents</text></navLabel>"
              '<content src="index.html"/></navPoint>']

    index = 0
    order = 1
    for slot, (feed_name, group) in enumerate(group_by_feed(articles)):
        first = index          # the feed entry opens its own first article
        order += 1
        feed_order = order     # parent and first child address the same page,
        children = []          # so per the NCX spec they share a playOrder
        for position, article in enumerate(group):
            if position:
                order += 1
            children.append(
                f'<navPoint id="nav-a{index}" playOrder="{order}">'
                f"<navLabel><text>{_esc(article.title)}</text></navLabel>"
                f'<content src="article_{index:03d}/index.html"/>'
                "</navPoint>")
            index += 1
        points.append(
            f'<navPoint id="nav-f{slot}" playOrder="{feed_order}">'
            f"<navLabel><text>{_esc(feed_name)}</text></navLabel>"
            f'<content src="article_{first:03d}/index.html"/>'
            + "".join(children) + "</navPoint>")

    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head><meta name="dtb:uid" content="{uid}"/>'
        '<meta name="dtb:depth" content="2"/></head>\n'
        f"  <docTitle><text>{_esc(title)}</text></docTitle>\n"
        "  <navMap>\n    " + "\n    ".join(points) + "\n  </navMap>\n"
        "</ncx>\n"
    )


def build(*, title: str, author: str, articles: list[Article],
          cover_path: Path | None, image_dir: Path, out_path: Path,
          language: str = "en", when: datetime | None = None,
          embed_images: bool = True) -> Path:
    """Write an EPUB 2 containing `articles`. Returns the output path."""
    when = when or datetime.now()
    uid = f"urn:uuid:{uuid.uuid4()}"
    # Contents, spine and NCX all index the same list, so grouping happens
    # once, here, and everything downstream stays consistent.
    articles = [a for _name, group in group_by_feed(articles) for a in group]
    has_cover = bool(embed_images and cover_path and cover_path.exists())

    pages: list[tuple[str, str]] = []          # (path, xhtml)
    blobs: list[tuple[str, bytes]] = []        # (path, bytes)
    image_entries: list[tuple[str, str]] = []  # (href, media type)
    dirs: list[str] = []

    for index, article in enumerate(articles):
        folder = f"article_{index:03d}"
        dirs.append(f"{folder}/")
        mapping = _plan_images(article, embed_images)

        wrote_any = False
        for stored, short in mapping.items():
            source = image_dir / stored
            if not source.exists():
                continue
            if not wrote_any:
                dirs.append(f"{folder}/images/")
                wrote_any = True
            href = f"{folder}/images/{short}"
            blobs.append((href, source.read_bytes()))
            media = "image/png" if short.endswith(".png") else "image/jpeg"
            image_entries.append((href, media))

        body = _chapter_html(article, index, embed_images, mapping)
        pages.append((f"{folder}/index.html",
                      PAGE.format(title=_esc(article.title),
                                  css="../stylesheet.css", body=body)))

    contents = PAGE.format(title=_esc(title), css="stylesheet.css",
                           body=_contents_html(articles))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype must be the first entry and stored uncompressed.
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/", b"")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("content.opf",
                   _opf(title, author, uid, when, articles, image_entries,
                        has_cover))
        z.writestr("toc.ncx", _ncx(title, uid, articles))
        z.writestr("stylesheet.css", CSS)
        z.writestr("index.html", contents)
        if has_cover:
            z.writestr("cover.jpg", cover_path.read_bytes())
        # Explicit directory entries, as calibre emits them; a simple zip
        # reader may expect them to be present.
        for folder in dirs:
            z.writestr(folder, b"")
        for path, text in pages:
            z.writestr(path, text)
        for path, data in blobs:
            z.writestr(path, data)

    log.info("built %s: %d article(s), %d image(s)",
             out_path.name, len(articles), len(image_entries))
    return out_path
