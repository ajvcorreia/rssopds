"""OPDS 1.2 Atom catalogue generation."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

from ..models import Category, Edition

OPDS_ACQUISITION = ("application/atom+xml;profile=opds-catalog;"
                    "kind=acquisition")
OPDS_NAVIGATION = ("application/atom+xml;profile=opds-catalog;"
                   "kind=navigation")
REL_ACQUIRE = "http://opds-spec.org/acquisition"
REL_IMAGE = "http://opds-spec.org/image"
REL_THUMB = "http://opds-spec.org/image/thumbnail"


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _attr(value: str) -> str:
    return quoteattr(value or "")


def _stamp(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stem(edition: Edition) -> str:
    """Filename an ereader should save this edition as, without .epub.

    Readers pick the saved name from (roughly, in order) Content-Disposition,
    the OPDS title, or the URL's last segment. All three are made distinct per
    edition so that a new edition never overwrites the previous one on device.
    """
    if edition.epub_file:
        return edition.epub_file.rsplit(".epub", 1)[0]
    return f"edition-{edition.id}"


def _link(rel: str, href: str, mime: str) -> str:
    return f'  <link rel={_attr(rel)} href={_attr(href)} type={_attr(mime)}/>\n'


def opds_root(*, base: str, title: str, publisher: str = "") -> str:
    """Top-level navigation feed: RSS and Ebooks as two folders.

    Kept deliberately tiny -- this is just a signpost. RSS is the existing
    per-category acquisition feed, unchanged apart from moving to its own
    URL; Ebooks is a plain folder browser over whatever sits on disk.
    """
    entries = (
        ("rss", "RSS", f"{base}/opds/rss", OPDS_ACQUISITION,
         "Articles from your subscribed feeds and Threads, one book per "
         "category, refreshed automatically."),
        ("ebooks", "Ebooks", f"{base}/opds/ebooks", OPDS_NAVIGATION,
         "Your own ebook library, organised in folders on the server."),
    )
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opds="http://opds-spec.org/2010/catalog">\n',
        '  <id>urn:opds:root</id>\n',
        f'  <title>{_esc(title)}</title>\n',
        f'  <updated>{_stamp(None)}</updated>\n',
        f'  <author><name>{_esc(publisher or title)}</name></author>\n',
        _link("self", f"{base}/opds", OPDS_NAVIGATION),
        _link("start", f"{base}/opds", OPDS_NAVIGATION),
    ]
    for entry_id, entry_title, href, kind_mime, summary in entries:
        out.append("  <entry>\n")
        out.append(f"    <id>urn:opds:{entry_id}</id>\n")
        out.append(f"    <title>{_esc(entry_title)}</title>\n")
        out.append(f"    <updated>{_stamp(None)}</updated>\n")
        out.append(f'    <content type="text">{_esc(summary)}</content>\n')
        out.append(f'    <link rel="subsection" href={_attr(href)} '
                   f'type={_attr(kind_mime)}/>\n')
        out.append("  </entry>\n")
    out.append("</feed>\n")
    return "".join(out)


def ebooks_nav(*, base: str, path: str, dirs: list[str],
               files: list[tuple[str, int, str]]) -> str:
    """Folder listing under /opds/ebooks/<path>.

    `path` is the current folder, POSIX-style and without a leading or
    trailing slash ("" for the root). `dirs` are subfolder names; `files` are
    (name, size_bytes, mime_type). Purely a mirror of the filesystem -- no
    covers, no read tracking, nothing generated or remembered about it.
    """
    parts = [p for p in path.split("/") if p]
    title = parts[-1] if parts else "Ebooks"
    self_href = f"{base}/opds/ebooks" + (f"/{quote(path)}" if path else "")
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opds="http://opds-spec.org/2010/catalog">\n',
        f'  <id>urn:opds:ebooks:{_esc(path or "root")}</id>\n',
        f'  <title>{_esc(title)}</title>\n',
        f'  <updated>{_stamp(None)}</updated>\n',
        _link("self", self_href, OPDS_NAVIGATION),
        _link("start", f"{base}/opds", OPDS_NAVIGATION),
    ]
    if parts:
        up = "/".join(parts[:-1])
        up_href = f"{base}/opds/ebooks" + (f"/{quote(up)}" if up else "")
        out.append(_link("up", up_href, OPDS_NAVIGATION))

    for name in dirs:
        child = f"{path}/{name}" if path else name
        out.append("  <entry>\n")
        out.append(f"    <id>urn:opds:ebooks-dir:{_esc(child)}</id>\n")
        out.append(f"    <title>{_esc(name)}</title>\n")
        out.append(f"    <updated>{_stamp(None)}</updated>\n")
        out.append(f'    <link rel="subsection" '
                   f'href={_attr(f"{base}/opds/ebooks/{quote(child)}")} '
                   f'type={_attr(OPDS_NAVIGATION)}/>\n')
        out.append("  </entry>\n")

    for name, size, mime in files:
        child = f"{path}/{name}" if path else name
        stem = name.rsplit(".", 1)[0] if "." in name else name
        out.append("  <entry>\n")
        out.append(f"    <id>urn:opds:ebooks-file:{_esc(child)}</id>\n")
        out.append(f"    <title>{_esc(stem)}</title>\n")
        out.append(f"    <updated>{_stamp(None)}</updated>\n")
        out.append(f'    <link rel={_attr(REL_ACQUIRE)} '
                   f'href={_attr(f"{base}/opds/ebooks-file/{quote(child)}")} '
                   f'type={_attr(mime)} length="{size}"/>\n')
        out.append("  </entry>\n")

    if not dirs and not files:
        out.append("  <entry>\n"
                   "    <id>urn:opds:ebooks-empty</id>\n"
                   "    <title>Nothing here yet</title>\n"
                   f"    <updated>{_stamp(None)}</updated>\n"
                   '    <content type="text">Drop files into this folder on '
                   "the server to see them here.</content>\n"
                   "  </entry>\n")

    out.append("</feed>\n")
    return "".join(out)


def rss_catalog(*, title: str, base: str, entries: list[tuple[Category, Edition]],
                article_titles: dict[int, list[str]], publisher: str = "",
                updated: datetime | None = None) -> str:
    """The RSS acquisition feed: one book per category that has unread articles.

    A category with no available edition simply is not emitted, which is what
    makes empty sections disappear from the reader.

    Each entry's author is the category it came from, or `publisher` when one
    is configured. Readers build the saved filename from the author and title,
    so this deliberately never carries this application's name.
    """
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:dc="http://purl.org/dc/terms/" '
        'xmlns:opds="http://opds-spec.org/2010/catalog">\n',
        f'  <id>urn:opds:catalog</id>\n',
        f'  <title>{_esc(title)}</title>\n',
        f'  <updated>{_stamp(updated)}</updated>\n',
        f'  <author><name>{_esc(publisher or title)}</name></author>\n',
        _link("self", f"{base}/opds/rss", OPDS_ACQUISITION),
        _link("start", f"{base}/opds", OPDS_NAVIGATION),
    ]

    for category, edition in entries:
        titles = article_titles.get(edition.id, [])
        summary = "\n".join(f"<li>{_esc(t)}</li>" for t in titles[:40])
        more = ("" if len(titles) <= 40
                else f"<p>and {len(titles) - 40} more</p>")
        cover_href = f"{base}/opds/cover/{category.id}.jpg"

        out.append("  <entry>\n")
        out.append(f"    <id>urn:opds:edition:{edition.id}</id>\n")
        out.append(f"    <title>{_esc(edition.title)}</title>\n")
        out.append(f"    <updated>{_stamp(edition.created_at)}</updated>\n")
        out.append(f'    <author><name>{_esc(publisher or category.name)}'
                   f'</name></author>\n')
        out.append(f"    <dc:issued>{_stamp(edition.created_at)}</dc:issued>\n")
        out.append(f'    <category label={_attr(category.name)} '
                   f'term={_attr(category.slug)}/>\n')
        out.append('    <content type="xhtml">'
                   '<div xmlns="http://www.w3.org/1999/xhtml">'
                   f'<p>{edition.article_count} unread</p>'
                   f'<ul>{summary}</ul>{more}</div></content>\n')
        out.append(f'    <link rel={_attr(REL_ACQUIRE)} '
                   f'href={_attr(f"{base}/opds/edition/{edition.id}/{_stem(edition)}.epub")} '
                   f'type="application/epub+zip" '
                   f'length="{edition.size_bytes}"/>\n')
        out.append(f'    <link rel={_attr(REL_IMAGE)} '
                   f'href={_attr(cover_href)} type="image/jpeg"/>\n')
        out.append(f'    <link rel={_attr(REL_THUMB)} '
                   f'href={_attr(cover_href)} type="image/jpeg"/>\n')
        out.append("  </entry>\n")

    if not entries:
        # An empty catalogue still has to be valid Atom, and a reader showing
        # "Nothing new" beats one showing a parse error.
        out.append("  <entry>\n"
                   "    <id>urn:opds:empty</id>\n"
                   "    <title>Nothing new</title>\n"
                   f"    <updated>{_stamp(updated)}</updated>\n"
                   '    <content type="text">Everything has been read.'
                   "</content>\n"
                   "  </entry>\n")

    out.append("</feed>\n")
    return "".join(out)
