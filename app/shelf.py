"""Shared filesystem logic for the "dumb" drop-folder shelves.

Both the Ebooks shelf (data/ebooks/) and the Files shelf (data/files/) are
plain folders the user drops things into directly, mirrored read-only over
OPDS and manageable from the web UI -- no AI cleaning, no read-tracking,
nothing generated except what each shelf explicitly opts into. app/ebooks.py
and app/files.py each wrap one Shelf instance around their own directory (and,
for Ebooks, its format-specific MIME table) so path-safety only has to be
gotten right once.
"""
from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class Shelf:
    def __init__(self, base_dir: Path, mime_overrides: dict[str, str] | None = None):
        self.base_dir = base_dir
        self._mime_overrides = mime_overrides or {}

    def guess_mime(self, path: Path) -> str:
        override = self._mime_overrides.get(path.suffix.lower())
        return override or mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def resolve(self, subpath: str) -> Path | None:
        """Resolve a URL subpath to a real path strictly inside base_dir.

        Checked before it ever reaches pathlib's `/` operator: joining onto an
        absolute right-hand operand silently discards the left side entirely,
        so a subpath of "/etc/passwd" would otherwise become a real
        filesystem escape rather than a rejected request. Returns None for
        anything that doesn't resolve safely inside the shelf.
        """
        rel = PurePosixPath(subpath)
        if rel.is_absolute() or ".." in rel.parts:
            return None
        base = self.base_dir.resolve()
        target = (base / subpath).resolve()
        if target != base and base not in target.parents:
            return None
        return target

    def list_dir(self, target: Path) -> tuple[list[str], list[tuple[str, int, str]]]:
        """Return (subfolder names, [(filename, size_bytes, mime), ...]), sorted."""
        dirs: list[str] = []
        files: list[tuple[str, int, str]] = []
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.is_file():
                files.append((entry.name, entry.stat().st_size, self.guess_mime(entry)))
        return dirs, files

    def list_folders(self) -> list[str]:
        """Every folder under the shelf, as relative POSIX paths ("" is the root).

        Powers the "move to" destination picker -- small enough a shelf like
        this to just list them all rather than browse one level at a time.
        """
        base = self.base_dir.resolve()
        out = [""]

        def walk(dir_path: Path, prefix: str) -> None:
            for entry in sorted(dir_path.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith("."):
                    child = f"{prefix}/{entry.name}" if prefix else entry.name
                    out.append(child)
                    walk(entry, child)

        if base.is_dir():
            walk(base, "")
        return out


def is_safe_name(name: str) -> bool:
    """A single path segment: no slashes, no "..", not empty."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def added_at(path: Path) -> datetime:
    """The file's mtime, as the closest thing a shelf has to a date added.

    Nothing else records one: a plain filesystem mirror has no database row
    to put it in. This is the upload time for anything added through the web
    UI (a fresh write always sets it), and whatever scp/rsync/docker cp
    preserved for anything dropped in directly -- usually the original
    file's own mtime, not the copy time, unless the transfer was told not to
    preserve it. A move within the shelf keeps it, since that's a rename on
    the same filesystem rather than a fresh write.
    """
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
