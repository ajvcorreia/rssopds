"""Fetch the real article page when the feed only carries a teaser."""
from __future__ import annotations

import logging

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup

from . import clean

log = logging.getLogger(__name__)

# Below this, a feed's own content is assumed to be a truncated teaser and the
# full page is worth fetching.
TEASER_WORDS = 120


def needs_fulltext(feed_html: str | None) -> bool:
    return clean.word_count(feed_html or "") < TEASER_WORDS


def hero_from_page(page_html: str, base_url: str) -> str | None:
    """The article's own share image, from og:image / twitter:image.

    Feeds often advertise only a thumbnail -- BBC's media:thumbnail is 240px
    wide -- which is close to useless on a 1400px e-ink screen. The social
    preview tag is the full-size lead image and costs nothing extra, since the
    page has already been fetched for the body text.
    """
    soup = BeautifulSoup(page_html, "lxml")
    candidates = [
        ("meta", {"property": "og:image"}),
        ("meta", {"property": "og:image:url"}),
        ("meta", {"name": "twitter:image"}),
        ("meta", {"name": "twitter:image:src"}),
        ("link", {"rel": "image_src"}),
    ]
    for tag_name, attrs in candidates:
        tag = soup.find(tag_name, attrs=attrs)
        if not tag:
            continue
        value = tag.get("content") or tag.get("href")
        if value and not value.startswith("data:"):
            return urljoin(base_url, value.strip())
    return None


def fetch_page(url: str, *, user_agent: str, timeout: int) -> str | None:
    """Fetch an article page once, so body and hero come from the same GET."""
    if not url:
        return None
    try:
        resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout,
                         follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("page fetch failed for %s: %s", url, exc)
        return None
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and ctype:
        return None
    return resp.text


def extract_body(page_html: str, url: str) -> str | None:
    """Readability-style extraction. Returns HTML, or None if it went badly."""
    try:
        extracted = trafilatura.extract(
            page_html,
            url=url,
            output_format="html",
            include_images=True,
            include_links=False,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
    except Exception as exc:  # trafilatura raises a variety of parser errors
        log.info("extraction failed for %s: %s", url, exc)
        return None

    if not extracted or clean.word_count(extracted) < 40:
        return None
    return extracted


@dataclass(slots=True)
class Extraction:
    body_html: str
    hero_url: str | None = None


def best_body(item_html: str | None, url: str | None, *, allow_fetch: bool,
              user_agent: str, timeout: int) -> Extraction:
    """Fullest available body, plus the page's own lead image if we fetched it."""
    have = item_html or ""
    if not allow_fetch or not url or not needs_fulltext(have):
        return Extraction(have)

    page = fetch_page(url, user_agent=user_agent, timeout=timeout)
    if page is None:
        return Extraction(have)

    body = extract_body(page, url)
    if body and clean.word_count(body) > clean.word_count(have):
        have = body
    return Extraction(have, hero_from_page(page, url))
