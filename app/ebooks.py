"""Filesystem helpers for the dumb Ebooks shelf (data/ebooks/).

Shared between the OPDS browser (app/opds/routes.py) and the web UI's upload
page (app/web/routes.py) so path-safety only has to be gotten right once.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path, PurePosixPath

from .config import config

# mimetypes' built-in table doesn't know most ebook formats.
MIME_OVERRIDES = {
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
    ".azw": "application/vnd.amazon.ebook",
    ".azw3": "application/vnd.amazon.ebook",
    ".cbz": "application/vnd.comicbook+zip",
    ".cbr": "application/vnd.comicbook-rar",
    ".fb2": "application/x-fictionbook+xml",
}


def guess_mime(path: Path) -> str:
    override = MIME_OVERRIDES.get(path.suffix.lower())
    return override or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def resolve(subpath: str) -> Path | None:
    """Resolve a URL subpath to a real path strictly inside ebooks_dir.

    Checked before it ever reaches pathlib's `/` operator: joining onto an
    absolute right-hand operand silently discards the left side entirely, so
    a subpath of "/etc/passwd" would otherwise become a real filesystem
    escape rather than a rejected request. Returns None for anything that
    doesn't resolve safely inside the shelf.
    """
    rel = PurePosixPath(subpath)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    base = config.ebooks_dir.resolve()
    target = (base / subpath).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def is_safe_name(name: str) -> bool:
    """A single path segment: no slashes, no "..", not empty."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def list_dir(target: Path) -> tuple[list[str], list[tuple[str, int, str]]]:
    """Return (subfolder names, [(filename, size_bytes, mime), ...]), sorted."""
    dirs: list[str] = []
    files: list[tuple[str, int, str]] = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            dirs.append(entry.name)
        elif entry.is_file():
            files.append((entry.name, entry.stat().st_size, guess_mime(entry)))
    return dirs, files
