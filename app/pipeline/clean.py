"""Turn source HTML into e-ink-friendly HTML.

Two stages. The rule-based sanitiser always runs and is what actually
guarantees the output is safe and small. The Ollama pass is optional polish on
top, and it is always second-guessed: if the model gives back materially less
text than it was given, its answer is thrown away and the rule-based result is
used instead. An LLM quietly summarising an article it was asked to tidy is the
single most likely way this pipeline could corrupt someone's reading, so it is
never trusted blind.
"""
from __future__ import annotations

import logging
import re
import threading

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

log = logging.getLogger(__name__)

# Tags worth keeping in an EPUB. Everything else is unwrapped or dropped.
KEEP_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "em", "strong", "i", "b", "sub", "sup",
    "figure", "figcaption", "img",
    "table", "thead", "tbody", "tr", "th", "td",
}

# Structural furniture that never belongs in a reading copy.
DROP_TAGS = {
    "script", "style", "noscript", "iframe", "form", "input", "button",
    "select", "textarea", "svg", "canvas", "video", "audio", "object",
    "embed", "nav", "aside", "header", "footer", "menu", "dialog",
}

KEEP_ATTRS = {"img": {"src", "alt"}, "td": {"colspan", "rowspan"},
              "th": {"colspan", "rowspan"}}

BOILERPLATE = re.compile(
    r"share|social|newsletter|subscrib|related|promo|advert|sponsor|cookie|"
    r"consent|comment|sidebar|breadcrumb|byline|tags?-list|pagination|"
    r"read-more|more-stories|trending|paywall|donate|follow-us|back-to-top",
    re.I,
)

BOILERPLATE_TEXT = re.compile(
    r"^\s*(share this|read more|advertisement|sign up|subscribe|follow us|"
    r"related stories|continue reading|view comments?)\b", re.I,
)

# Trailing "1/7", "(2/9)", "🧵" markers people put on thread parts.
THREAD_MARKER = re.compile(
    r"(?:^|\s)[\(\[]?\d{1,2}\s*/\s*\d{1,2}[\)\]]?(?=\s|$)|\U0001F9F5", re.U)


def _drop_boilerplate(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        # find_all materialises the list up front, so a tag may already have
        # gone when its ancestor was decomposed; touching it then raises.
        if tag.decomposed:
            continue
        if tag.name in DROP_TAGS:
            tag.decompose()
            continue
        ident = " ".join(filter(None, [
            " ".join(tag.get("class") or []), tag.get("id") or "",
            tag.get("role") or "", tag.get("data-testid") or "",
        ]))
        if ident and BOILERPLATE.search(ident):
            tag.decompose()


def _strip_tracking_images(soup: BeautifulSoup) -> None:
    for img in soup.find_all("img"):
        if img.decomposed:
            continue
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        # 1x1 beacons.
        try:
            if (int(img.get("width", 99)) <= 2) or (int(img.get("height", 99)) <= 2):
                img.decompose()
                continue
        except (TypeError, ValueError):
            pass
        # Lazy-loaded images keep the real URL in data-src.
        if not img.get("src") and img.get("data-src"):
            img["src"] = img["data-src"]


def _normalise_tags(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        if tag.name == "a":
            # Links are dead weight on an offline reader: keep the words, drop
            # the destination.
            tag.unwrap()
            continue
        if tag.name not in KEEP_TAGS:
            tag.unwrap()
            continue
        allowed = KEEP_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]


def _drop_empties(soup: BeautifulSoup) -> None:
    changed = True
    while changed:
        changed = False
        for tag in soup.find_all(["p", "div", "span", "li", "blockquote",
                                  "figure", "figcaption"]):
            if tag.decomposed:
                continue
            if tag.find("img"):
                continue
            text = tag.get_text(strip=True)
            if not text:
                tag.decompose()
                changed = True
            elif BOILERPLATE_TEXT.match(text) and len(text) < 120:
                tag.decompose()
                changed = True


def sanitize(html: str) -> str:
    """Rule-based clean. Always safe, never loses body text."""
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "lxml")
    _drop_boilerplate(soup)
    _strip_tracking_images(soup)
    _normalise_tags(soup)
    _drop_empties(soup)

    body = soup.body or soup
    out = "".join(str(c) for c in body.children).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def strip_thread_markers(html: str) -> str:
    """Remove "3/9" style counters left over from a merged self-thread."""
    soup = BeautifulSoup(html, "lxml")
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        # Leave verbatim blocks alone -- "1/2" there is probably real content.
        if isinstance(parent, Tag) and parent.name in ("pre", "code"):
            continue
        replaced = THREAD_MARKER.sub(" ", str(node))
        if replaced != str(node):
            node.replace_with(re.sub(r"\s{2,}", " ", replaced))
    body = soup.body or soup
    return "".join(str(c) for c in body.children).strip()


def text_of(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def word_count(html: str) -> int:
    return len(text_of(html).split())


# --- Ollama pass ----------------------------------------------------------

PROMPT = """You are cleaning an article for display on an e-ink e-reader.

Rules, in order of importance:
1. Reproduce the body text VERBATIM. Do not summarise, shorten, rewrite,
   translate or add commentary. Every sentence of the original must appear.
2. Delete only non-article furniture: navigation, share prompts, newsletter
   signups, cookie notices, advertisement labels, "related stories" lists,
   author bio blocks, and repeated site names.
3. Return clean HTML using only these tags: p, h2, h3, ul, ol, li, blockquote,
   em, strong, figure, figcaption, img, pre, code.
4. Keep any <img> tags exactly as they are.
5. Output the HTML only. No preamble, no code fence, no explanation.

Article HTML follows.
---
{chunk}"""

FENCE = re.compile(r"^\s*```(?:html)?\s*|\s*```\s*$", re.I)


def _chunks(html: str, budget_chars: int) -> list[str]:
    """Split on top-level blocks so no element is cut in half."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup
    blocks = [str(c) for c in body.children if str(c).strip()]
    out: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) > budget_chars:
            out.append(current)
            current = block
        else:
            current += block
    if current:
        out.append(current)
    return out or [html]


# One request in flight at a time. A local Ollama serving a multi-GB model has
# no spare capacity for parallel work: concurrent requests queue inside the
# server anyway, but each caller's clock is already running, so they all time
# out together. Serialising means one request waits for the model to load and
# the rest then find it warm.
_OLLAMA_LOCK = threading.Lock()


def _ollama_generate(base_url: str, model: str, prompt: str, timeout: int,
                     num_ctx: int, keep_alive: str = "30m") -> str:
    with _OLLAMA_LOCK:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                # Without keep_alive the model is evicted between articles and
                # every request pays the multi-GB load cost again.
                "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_ctx": num_ctx},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return (resp.json().get("response") or "").strip()


def ai_clean(html: str, *, base_url: str, model: str, timeout: int,
             num_ctx: int, min_retain: float,
             keep_alive: str = "30m") -> tuple[str, str]:
    """Return (html, cleaned_by). Falls back to the sanitised input on any doubt."""
    base = sanitize(html)
    if not base:
        return "", "rules"

    original_words = word_count(base)
    if original_words < 40:
        # Short posts (most Threads content) are not worth a model round trip.
        return base, "rules"

    # Roughly 3.5 chars per token, and leave half the window for the answer.
    budget = max(1500, int(num_ctx * 3.5 * 0.45))

    pieces: list[str] = []
    try:
        for chunk in _chunks(base, budget):
            raw = _ollama_generate(base_url, model, PROMPT.format(chunk=chunk),
                                   timeout, num_ctx, keep_alive)
            pieces.append(FENCE.sub("", raw))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("ollama clean failed, using rule-based output: %s", exc)
        return base, "rules"

    candidate = sanitize("\n".join(pieces))
    if not candidate:
        return base, "rules"

    ratio = word_count(candidate) / max(original_words, 1)
    if ratio < min_retain:
        log.warning("ollama returned %.0f%% of the text (min %.0f%%); "
                    "discarding its output", ratio * 100, min_retain * 100)
        return base, "rules"

    return candidate, model


# Characters a small e-ink font is unlikely to carry, mapped to something it
# certainly does. Deliberately narrow: ordinary punctuation -- en and em
# dashes, curly quotes, ellipses, bullets -- lives in General Punctuation
# (U+2010-U+205F) and is NOT touched, because those render fine and are real
# typography. Accented letters and non-Latin scripts are never touched either.
SYMBOL_REPLACEMENTS = {
    "←": "<-", "→": "->", "↔": "<->",
    "↑": "^", "↓": "v",
    "⇐": "<=", "⇒": "=>", "⇔": "<=>",
    "➔": ">", "➙": ">", "➜": ">", "➞": ">",
    "➡": ">", "➤": ">", "➧": ">", "➨": ">",
    "⬅": "<", "⮕": ">",
    "✓": "[x]", "✔": "[x]", "✗": "[ ]", "✘": "[ ]",
    "★": "*", "☆": "*", "✶": "*", "✻": "*",
    "●": "*", "○": "*", "■": "*", "□": "*",
    "▶": ">", "◀": "<",
    "❤": "<3",
}

# Blocks that are decoration rather than text. Anything in here without an
# explicit replacement above is dropped: it would only render as an empty box.
DECORATIVE_RANGES = (
    (0x2190, 0x21FF),    # arrows
    (0x2500, 0x257F),    # box drawing
    (0x2580, 0x259F),    # block elements
    (0x25A0, 0x25FF),    # geometric shapes
    (0x2600, 0x26FF),    # miscellaneous symbols
    (0x2700, 0x27BF),    # dingbats -- U+27A7 lives here
    (0x2B00, 0x2BFF),    # miscellaneous symbols and arrows
    (0xFE00, 0xFE0F),    # variation selectors
    (0x1F000, 0x1FAFF),  # emoji, cards, dominoes
)

VERBATIM_TAGS = ("pre", "code")


def _is_decorative(ch: str) -> bool:
    cp = ord(ch)
    return any(low <= cp <= high for low, high in DECORATIVE_RANGES)


def simplify_symbols_text(text: str) -> str:
    """Replace decorative symbols in a plain string."""
    out = []
    for ch in text:
        if ch in SYMBOL_REPLACEMENTS:
            out.append(SYMBOL_REPLACEMENTS[ch])
        elif ch == "‍":          # zero-width joiner, glues emoji together
            continue
        elif _is_decorative(ch):
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"[ 	]{2,}", " ", "".join(out))


def simplify_symbols(html: str) -> str:
    """Same, over an HTML fragment, leaving pre/code blocks alone.

    A reader whose font lacks a glyph draws an empty box, so "sorted by date
    ➧➧➧" arrives as three boxes. Mapping the handful of
    decorative characters to ASCII is far more useful than the boxes, and
    dropping the rest beats showing them.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if isinstance(parent, Tag) and parent.name in VERBATIM_TAGS:
            continue
        replaced = simplify_symbols_text(str(node))
        if replaced != str(node):
            node.replace_with(replaced)
    body = soup.body or soup
    return "".join(str(c) for c in body.children).strip()


def strip_images(html: str) -> str:
    """Drop every image, for feeds configured text-only."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["img", "figure", "figcaption"]):
        tag.decompose()
    body = soup.body or soup
    return "".join(str(c) for c in body.children).strip()
