"""Cover thumbnails for the Ebooks shelf, extracted from the files themselves.

Unlike app/pipeline/covers.py (which generates a placeholder for an RSS
category), there is nothing to generate here -- either the file embeds a
cover image or it doesn't. EPUB and CBZ are both zip archives, so both are
supported; anything else (MOBI/AZW, FB2, CBR, ...) simply has no thumbnail,
which the web UI treats the same as extraction failing.
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .images import fit_within

log = logging.getLogger(__name__)

OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _epub_cover_bytes(zf: zipfile.ZipFile) -> bytes | None:
    """Find and read an EPUB's cover image via its OPF manifest."""
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path = container.find(f".//{{{CONTAINER_NS}}}rootfile").get("full-path")
        opf_root = ET.fromstring(zf.read(opf_path))
    except (KeyError, ET.ParseError, AttributeError):
        return None

    opf_dir = posixpath.dirname(opf_path)
    items = opf_root.findall(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item")

    # EPUB 3: the manifest item itself is flagged as the cover.
    href = next((i.get("href") for i in items
                if "cover-image" in (i.get("properties") or "").split()), None)

    # EPUB 2: <meta name="cover" content="some-manifest-id"/> points at it.
    if href is None:
        metas = opf_root.findall(f".//{{{OPF_NS}}}metadata/{{{OPF_NS}}}meta")
        cover_id = next((m.get("content") for m in metas
                         if m.get("name") == "cover"), None)
        if cover_id:
            href = next((i.get("href") for i in items
                        if i.get("id") == cover_id), None)

    # Last resort: an image manifest item with "cover" in its filename.
    if href is None:
        href = next((i.get("href") for i in items
                    if (i.get("media-type") or "").startswith("image/")
                    and "cover" in (i.get("href") or "").lower()), None)

    if href is None:
        return None
    try:
        return zf.read(posixpath.normpath(posixpath.join(opf_dir, href)))
    except KeyError:
        return None


def _cbz_cover_bytes(zf: zipfile.ZipFile) -> bytes | None:
    """A CBZ has no manifest -- its cover is just the first page, by name."""
    names = sorted(n for n in zf.namelist()
                   if not n.endswith("/") and Path(n).suffix.lower() in IMAGE_SUFFIXES)
    return zf.read(names[0]) if names else None


def _extract(path: Path) -> bytes | None:
    suffix = path.suffix.lower()
    if suffix not in (".epub", ".cbz"):
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            return (_epub_cover_bytes(zf) if suffix == ".epub"
                    else _cbz_cover_bytes(zf))
    except (zipfile.BadZipFile, OSError):
        return None


def thumbnail(path: Path, cache_dir: Path, *, width: int = 96,
             height: int = 144) -> Path | None:
    """A cached JPEG thumbnail of `path`'s embedded cover, or None.

    Cached under a name derived from the file's identity and mtime, so an
    edited or replaced file gets a fresh thumbnail; the old cache entry for
    it, and any left behind by a since-moved-or-deleted file, simply goes
    unused rather than being tracked and cleaned up -- thumbnails are a few
    KB each, and this is a personal shelf, not a scale that needs it.
    """
    try:
        stat = path.stat()
    except OSError:
        return None

    key = f"{path.resolve()}|{stat.st_mtime_ns}|{width}x{height}"
    dest = cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.jpg"
    if dest.exists():
        return dest

    raw = _extract(path)
    if raw is None:
        return None

    try:
        with Image.open(BytesIO(raw)) as img:
            img = img.convert("RGB")
            img = fit_within(img, width, height)
            cache_dir.mkdir(parents=True, exist_ok=True)
            img.save(dest, "JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError):
        log.info("could not decode embedded cover for %s", path.name)
        return None
    return dest
