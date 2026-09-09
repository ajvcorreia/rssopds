"""Download and downscale the one image an article gets."""
from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
# Below this in BOTH dimensions an image is an icon, avatar or spacer rather
# than article art. Kept well under the smallest sensible screen so a genuine
# picture is never discarded on a low-resolution device.
MIN_DIMENSION = 80


def fit_within(img, max_width: int, max_height: int):
    """Scale down to fit inside the box, preserving aspect ratio.

    Only ever shrinks: enlarging a small image wastes bytes and looks worse.
    Both bounds matter -- capping width alone lets a tall portrait image run
    far off the bottom of a short screen.
    """
    max_width = max(1, max_width)
    max_height = max(1, max_height)
    scale = min(max_width / img.width, max_height / img.height, 1.0)
    if scale >= 1.0:
        return img
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.LANCZOS)


def first_image_in(html: str | None) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    img = soup.find("img")
    return img.get("src") if img else None


def download(url: str, dest_dir: Path, *, user_agent: str, timeout: int,
             max_width: int, max_height: int, quality: int,
             grayscale: bool) -> str | None:
    """Fetch, normalise and store an image. Returns the stored filename."""
    if not url or url.startswith("data:"):
        return None

    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ".jpg"
    dest = dest_dir / name
    if dest.exists():
        return name

    try:
        with httpx.stream("GET", url, headers={"User-Agent": user_agent},
                          timeout=timeout, follow_redirects=True) as resp:
            resp.raise_for_status()
            buf = BytesIO()
            for block in resp.iter_bytes():
                buf.write(block)
                if buf.tell() > MAX_DOWNLOAD_BYTES:
                    log.info("image too large, skipping: %s", url)
                    return None
    except httpx.HTTPError as exc:
        log.info("image download failed for %s: %s", url, exc)
        return None

    buf.seek(0)
    try:
        with Image.open(buf) as img:
            img = ImageOps.exif_transpose(img)
            if img.width < MIN_DIMENSION and img.height < MIN_DIMENSION:
                return None
            img = img.convert("L" if grayscale else "RGB")
            img = fit_within(img, max_width, max_height)
            # Baseline JPEG only. Progressive JPEGs are fine in a browser but
            # a large share of e-readers -- anything on Adobe RMSDK, so Kobo,
            # Nook, Sony -- cannot decode them and render nothing at all. The
            # image is in the book, correctly referenced, and simply invisible.
            img.save(dest, "JPEG", quality=quality, optimize=True,
                     progressive=False)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.info("image decode failed for %s: %s", url, exc)
        dest.unlink(missing_ok=True)
        return None

    return name


def jpeg_encoding(data: bytes) -> str:
    """BASELINE (SOF0/SOF1), PROGRESSIVE (SOF2), or UNKNOWN, from the markers."""
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1):
            return "BASELINE"
        if marker == 0xC2:
            return "PROGRESSIVE"
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return "UNKNOWN"


def jpeg_components(data: bytes) -> int:
    """Number of colour components: 1 = greyscale, 3 = YCbCr."""
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            # SOF: length(2) precision(1) height(2) width(2) components(1)
            return data[i + 9]
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return 0


def repair_greyscale(image_dir: Path) -> int:
    """Re-encode single-component JPEGs as 3-component.

    A greyscale JPEG has one colour component, and a good many small decoders
    -- the sort found on microcontroller readers -- only implement 3-component
    YCbCr and silently render nothing. Converting to RGB looks identical on an
    e-ink screen and costs a little size, so it is the safe default. Cached
    files are never re-downloaded, so existing ones are converted in place.
    """
    repaired = 0
    for path in image_dir.glob("*.jpg"):
        try:
            data = path.read_bytes()
            if jpeg_components(data) != 1:
                continue
            with Image.open(BytesIO(data)) as img:
                img.load()
                img.convert("RGB").save(path, "JPEG", quality=85,
                                        optimize=True, progressive=False)
            repaired += 1
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            log.warning("could not re-encode %s: %s", path.name, exc)
    if repaired:
        log.info("re-encoded %d greyscale JPEG(s) as 3-component", repaired)
    return repaired


def repair_oversized(image_dir: Path, max_width: int, max_height: int) -> int:
    """Shrink cached images that exceed the configured screen box.

    Images are cached by URL hash and never re-fetched, so lowering the size
    limit would otherwise only affect articles yet to arrive. Note this is
    lossy and one-way: the originals are not kept, so raising the limit again
    will not restore detail for images already stored.
    """
    resized = 0
    for path in image_dir.glob("*.jpg"):
        try:
            with Image.open(path) as img:
                img.load()
                if img.width <= max_width and img.height <= max_height:
                    continue
                fit_within(img, max_width, max_height).save(
                    path, "JPEG", quality=85, optimize=True, progressive=False)
            resized += 1
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            log.warning("could not resize %s: %s", path.name, exc)
    if resized:
        log.info("shrank %d cached image(s) to fit %dx%d",
                 resized, max_width, max_height)
    return resized


def repair_progressive(image_dir: Path) -> int:
    """Re-encode already-stored progressive JPEGs as baseline.

    Images are cached by URL hash and never re-downloaded, so files written
    before the baseline fix would stay invisible on affected readers forever.
    Runs at startup; a no-op once everything is baseline.
    """
    repaired = 0
    for path in image_dir.glob("*.jpg"):
        try:
            data = path.read_bytes()
            if jpeg_encoding(data) != "PROGRESSIVE":
                continue
            with Image.open(BytesIO(data)) as img:
                img.load()
                img.save(path, "JPEG", quality=85, optimize=True,
                         progressive=False)
            repaired += 1
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            log.warning("could not re-encode %s: %s", path.name, exc)
    if repaired:
        log.info("re-encoded %d progressive JPEG(s) as baseline", repaired)
    return repaired


def rewrite_body_images(html: str, dest_dir: Path, *, user_agent: str,
                        timeout: int, max_width: int, max_height: int,
                        quality: int, grayscale: bool,
                        limit: int = 8) -> tuple[str, list[str]]:
    """Localise inline images so the EPUB is self-contained.

    Threads posts carry their pictures in the body rather than as a hero, so
    inline images are downloaded too -- up to `limit` of them. Images that fail
    to download have their tags removed rather than leaving a broken reference
    in the book.
    """
    if not html:
        return "", []

    soup = BeautifulSoup(html, "lxml")
    stored: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or len(stored) >= limit:
            img.decompose()
            continue
        name = download(src, dest_dir, user_agent=user_agent, timeout=timeout,
                        max_width=max_width, max_height=max_height,
                        quality=quality, grayscale=grayscale)
        if not name:
            img.decompose()
            continue
        img["src"] = f"images/{name}"
        stored.append(name)

    body = soup.body or soup
    return "".join(str(c) for c in body.children).strip(), stored
