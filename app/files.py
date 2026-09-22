"""Filesystem helpers for the dumb Files shelf (data/files/).

A second drop folder next to the Ebooks shelf, for anything that isn't a
book -- firmware images, PDFs, zips, whatever you want to pull onto a
reader's storage over OPDS or the web UI (e.g. downloading a firmware image
onto an e-reader's SD card so you can flash it from there). No format
assumptions and no cover thumbnails, unlike app/ebooks.py -- just files.
Shared with app/ebooks.py via app/shelf.py so path-safety only has to be
gotten right once.
"""
from __future__ import annotations

from .config import config
from .shelf import Shelf, added_at, is_safe_name  # noqa: F401 -- re-exported

_shelf = Shelf(config.files_dir)

guess_mime = _shelf.guess_mime
resolve = _shelf.resolve
list_dir = _shelf.list_dir
list_folders = _shelf.list_folders
