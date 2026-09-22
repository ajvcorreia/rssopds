"""Filesystem helpers for the dumb Ebooks shelf (data/ebooks/).

Shared between the OPDS browser (app/opds/routes.py) and the web UI's upload
page (app/web/routes.py) so path-safety only has to be gotten right once. The
actual path-safety and listing logic lives in app/shelf.py, shared with the
Files shelf (app/files.py) -- this module just points a Shelf at ebooks_dir
and adds the ebook-format MIME table.
"""
from __future__ import annotations

from .config import config
from .shelf import Shelf, added_at, is_safe_name  # noqa: F401 -- re-exported

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

_shelf = Shelf(config.ebooks_dir, MIME_OVERRIDES)

guess_mime = _shelf.guess_mime
resolve = _shelf.resolve
list_dir = _shelf.list_dir
list_folders = _shelf.list_folders
