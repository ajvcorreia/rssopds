"""EPUB structural validity, RSS ingest, and the cleaner's guard rails."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402

from app.config import config  # noqa: E402
from app.pipeline import clean  # noqa: E402
from app.pipeline.assemble import derive_title  # noqa: E402
from app.sources.rss import _entry_html, _pick_image, _to_dt  # noqa: E402

FAILS = []


def truthy(label, got):
    ok = bool(got)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        FAILS.append(label)


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


print("\n[A] EPUB structure")


def _build_sample_epub():
    """Build an EPUB here rather than relying on another test's leftovers.

    Uses a 4-part thread carrying a hero image that lives only on image_file,
    which is the shape RSS articles actually have.
    """
    from PIL import Image

    from app.pipeline import covers, epub as epubmod

    config.ensure_dirs()
    image_dir = config.image_dir
    hero = image_dir / "sample-hero.jpg"
    Image.new("L", (400, 300), 128).save(hero, "JPEG")
    cover = covers.cover_for("Sample", None, config.cover_dir,
                             when=__import__("datetime").datetime.now(), count=1)

    class _Feed:
        title = "Sample Feed"

    class _Article:
        title = "A merged thread with a picture"
        byline = "Someone"
        feed = _Feed()
        published_at = None
        word_count = 42
        part_count = 4
        body_html = "<p>Real body text about e-readers.</p>"
        image_file = hero.name

    out = config.epub_dir / "sample-structure-test.epub"
    return epubmod.build(title="Sample - test", author="Sample",
                         articles=[_Article()],
                         cover_path=cover, image_dir=image_dir, out_path=out)


sample = _build_sample_epub()
truthy("an epub was built", sample.exists())
if sample.exists():
    with zipfile.ZipFile(sample) as z:
        names = z.namelist()
        bad = z.testzip()
        check("zip integrity", bad, None)
        check("mimetype is first entry", names[0], "mimetype")
        check("mimetype content", z.read("mimetype"),
              b"application/epub+zip")
        # The mimetype entry must be STORED, not deflated, per the EPUB spec.
        check("mimetype stored uncompressed",
              z.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
        truthy("container.xml present", "META-INF/container.xml" in names)
        truthy("cover image embedded", "cover.jpg" in names)

        # The layout is copied from a calibre book that this project's target
        # reader renders images from. Each of these is a property that book
        # has and the previous ebooklib output did not.
        check("opf sits at the zip root, not in a subfolder",
              [n for n in names if n.endswith(".opf")], ["content.opf"])
        truthy("container points at the root opf",
               'full-path="content.opf"' in
               z.read("META-INF/container.xml").decode())
        opf_text = z.read("content.opf").decode("utf-8")
        truthy("declares EPUB 2", 'version="2.0"' in opf_text)
        truthy("has an ncx", "toc.ncx" in names)
        truthy("no EPUB/ content folder",
               not any(n.startswith("EPUB/") for n in names))
        truthy("contents page present", "index.html" in names)
        truthy("article folder present", "article_000/index.html" in names)
        truthy("cover declared in opf metadata", 'name="cover"' in opf_text)
        truthy("article listed in the manifest",
               'href="article_000/index.html"' in opf_text)
        truthy("article listed in the spine", 'idref="art0"' in opf_text)

        body = z.read("article_000/index.html").decode("utf-8")
        truthy("chapter has the thread note", "Thread of 4 posts" in body)
        truthy("chapter has body text", "e-readers" in body)
        # An image held only on article.image_file must be referenced AND
        # physically embedded, under a short calibre-style name.
        truthy("hero referenced relatively", 'src="images/img1.jpg"' in body)
        truthy("hero bytes embedded beside the chapter",
               "article_000/images/img1.jpg" in names)
        truthy("image is in the manifest",
               'href="article_000/images/img1.jpg"' in opf_text)
        truthy("no long hashed filenames leak into the book",
               "sample-hero.jpg" not in body)
        from lxml import etree
        for name in ("content.opf", "toc.ncx", "index.html",
                     "article_000/index.html"):
            try:
                etree.fromstring(z.read(name))
                truthy(f"{name} is well-formed XML", True)
            except etree.XMLSyntaxError as exc:
                truthy(f"{name} is well-formed XML ({exc})", False)
        truthy("no replacement characters", "�" not in body)

print("\n[A0b] contents and navigation are grouped by feed, with working links")
import re as _re  # noqa: E402

from app.pipeline import epub as _ep  # noqa: E402


def _mk(title, feed_title, published=None):
    class _F:
        pass

    class _A:
        pass

    f, a = _F(), _A()
    f.title = feed_title
    a.title, a.feed = title, f
    a.byline = a.published_at = None
    a.word_count, a.part_count = 10, 1
    a.body_html, a.image_file = "<p>Body.</p>", None
    return a


# Deliberately interleaved, to prove the builder regroups them.
mixed = [
    _mk("Alpha one", "Phoronix"),
    _mk("Beta one", "The Verge"),
    _mk("Alpha two", "Phoronix"),
    _mk("Gamma one", "NYT"),
    _mk("Beta two", "The Verge"),
]
grouped = _ep.group_by_feed(mixed)
check("feeds bucketed in first-seen order",
      [name for name, _ in grouped], ["Phoronix", "The Verge", "NYT"])
check("no article lost", sum(len(g) for _, g in grouped), len(mixed))

_toc = config.epub_dir / "toc-layout.epub"
_ep.build(title="Layout", author="Test", articles=mixed, cover_path=None,
          image_dir=config.image_dir, out_path=_toc, embed_images=False)

with zipfile.ZipFile(_toc) as z:
    idx = z.read("index.html").decode("utf-8")
    # A heading per feed, then that feed's articles beneath it.
    heads = _re.findall(r'<h2 class="feed"[^>]*>([^<]+)</h2>', idx)
    check("a heading per feed", heads, ["Phoronix", "The Verge", "NYT"])

    links = _re.findall(r'<li><a href="(article_\d+/index\.html)">([^<]+)</a></li>', idx)
    check("every article is a link", len(links), len(mixed))
    check("links are grouped under their feed",
          [t for _h, t in links],
          ["Alpha one", "Alpha two", "Beta one", "Beta two", "Gamma one"])
    truthy("nothing but the anchor inside each <li>",
           "<span" not in idx)

    # Each link must resolve to a file that is actually in the book.
    for href, _text in links:
        truthy(f"{href} exists in the package", href in z.namelist())

    # The spine order matches, so paging forward follows the same grouping.
    opf = z.read("content.opf").decode("utf-8")
    spine = _re.findall(r'<itemref idref="art(\d+)"/>', opf)
    check("spine follows the grouped order",
          spine, [str(i) for i in range(len(mixed))])
    first_body = z.read("article_000/index.html").decode("utf-8")
    truthy("first article is the first of the first feed",
           "Alpha one" in first_body)

    # And the NCX carries the same two-level structure.
    ncx = z.read("toc.ncx").decode("utf-8")
    truthy("ncx declares depth 2", 'content="2"' in ncx)
    check("ncx has a parent per feed", ncx.count('id="nav-f'), 3)
    check("ncx has a child per article", ncx.count('id="nav-a'), len(mixed))
    truthy("feed parents nest their articles",
           _re.search(r'id="nav-f0".*?id="nav-a0".*?</navPoint>', ncx,
                      _re.S) is not None)
    from lxml import etree as _etree
    try:
        _etree.fromstring(z.read("toc.ncx"))
        truthy("ncx is well-formed XML", True)
    except _etree.XMLSyntaxError as exc:
        truthy(f"ncx is well-formed XML ({exc})", False)
    try:
        _etree.fromstring(z.read("index.html"))
        truthy("contents page is well-formed XML", True)
    except _etree.XMLSyntaxError as exc:
        truthy(f"contents page is well-formed XML ({exc})", False)

print("\n[A1] images off produces a book with no image bytes at all")
from app.pipeline import epub as _epubmod  # noqa: E402


class _NoImgFeed:
    title = "A Feed"


class _NoImgArticle:
    title = "Article with a hero and an inline picture"
    byline = "Someone"
    feed = _NoImgFeed()
    published_at = None
    word_count = 42
    part_count = 1
    body_html = '<p>Text.</p><figure><img src="images/inline.jpg"/></figure>'
    image_file = "sample-hero.jpg"


_noimg = config.epub_dir / "images-off.epub"
_epubmod.build(title="No images", author="Test", articles=[_NoImgArticle()],
               cover_path=None, image_dir=config.image_dir, out_path=_noimg,
               embed_images=False)
with zipfile.ZipFile(_noimg) as z:
    entries = z.namelist()
    check("no image files in the package",
          [n for n in entries if n.endswith((".jpg", ".png"))], [])
    page = z.read("article_000/index.html").decode("utf-8")
    truthy("no img tags left in the chapter", "<img" not in page)
    truthy("the text is still there", "Text." in page)

print("\n[A0] images are scaled to fit the reader's screen box")
from app.pipeline import images as _im  # noqa: E402
from PIL import Image as _Img  # noqa: E402

SCREEN = (240, 480)
cases = [
    ("landscape 1200x675", (1200, 675), (240, 135)),
    ("portrait 1000x2000", (1000, 2000), (240, 480)),
    ("very tall 300x3000",  (300, 3000), (48, 480)),
    ("already small 200x100", (200, 100), (200, 100)),   # never enlarged
    ("exactly the screen", (240, 480), (240, 480)),
    ("square 900x900", (900, 900), (240, 240)),
]
for label, source, want in cases:
    out = _im.fit_within(_Img.new("RGB", source), *SCREEN)
    check(f"{label} -> fits", out.size, want)
    truthy(f"{label} stays inside {SCREEN[0]}x{SCREEN[1]}",
           out.width <= SCREEN[0] and out.height <= SCREEN[1])

# Aspect ratio must survive the scaling.
_orig = _Img.new("RGB", (1600, 900))
_fit = _im.fit_within(_orig, *SCREEN)
check("aspect ratio preserved",
      round(_fit.width / _fit.height, 2), round(1600 / 900, 2))

# Cached images from before the screen was configured get shrunk in place.
_big = config.image_dir / "oversized-cached.jpg"
_Img.new("RGB", (1200, 675), (120, 120, 120)).save(_big, "JPEG")
check("fixture starts oversized", _Img.open(_big).size, (1200, 675))
_n = _im.repair_oversized(config.image_dir, *SCREEN)
truthy("repair reported work", _n >= 1)
check("cached image shrunk to the screen", _Img.open(_big).size, (240, 135))
check("repair is idempotent",
      _im.repair_oversized(config.image_dir, *SCREEN), 0)

# The generated cover must lay out at the small size, not just be cropped.
from app.pipeline import covers as _cov  # noqa: E402

_small = config.cover_dir / "small-cover-test.jpg"
_cov.generate("A Long Category Name Here", _small,
              subtitle="Tuesday 09 September 2026", count=7,
              width=240, height=480)
check("cover rendered at the screen size", _Img.open(_small).size, (240, 480))
truthy("cover is a sane size on disk", 1000 < _small.stat().st_size < 120_000)

print("\n[A2] images must be baseline JPEG, not progressive")
# Progressive JPEGs decode fine in a browser but render as nothing on Adobe
# RMSDK readers (Kobo, Nook, Sony). The file is present and correctly
# referenced, so the only way to catch this is to inspect the encoding.


def jpeg_encoding(data: bytes) -> str:
    """BASELINE (SOF0/SOF1) vs PROGRESSIVE (SOF2), read from the markers."""
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


with zipfile.ZipFile(sample) as z:
    for name in [n for n in z.namelist() if n.endswith(".jpg")]:
        check(f"{name.split('/')[-1]} encoding",
              jpeg_encoding(z.read(name)), "BASELINE")

# Assert against the real writer, not just this one book. download() speaks
# HTTP, so drive it through a local server rather than reimplementing it.
import http.server  # noqa: E402
import threading  # noqa: E402

from PIL import Image as _Image  # noqa: E402

from app.pipeline import images as imagemod  # noqa: E402

_probe = config.image_dir / "probe-source.jpg"
_Image.new("RGB", (900, 600), (90, 120, 160)).save(
    _probe, "JPEG", progressive=True)  # a deliberately progressive source


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = _probe.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a):
        pass


_srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_stored = imagemod.download(
    f"http://127.0.0.1:{_srv.server_port}/hero.jpg", config.image_dir,
    user_agent="test", timeout=10, max_width=1200, max_height=1600,
    quality=80, grayscale=True)
_srv.shutdown()

truthy("writer stored the image", _stored)
if _stored:
    check("writer re-encodes progressive input as baseline",
          jpeg_encoding((config.image_dir / _stored).read_bytes()), "BASELINE")

print("\n[A2b] already-cached progressive images are repaired on startup")
_legacy = config.image_dir / "legacy-progressive.jpg"
_Image.new("L", (500, 300), 128).save(_legacy, "JPEG", progressive=True)
check("fixture really is progressive",
      jpeg_encoding(_legacy.read_bytes()), "PROGRESSIVE")
_n = imagemod.repair_progressive(config.image_dir)
truthy("repair reported work", _n >= 1)
check("cached image now baseline",
      jpeg_encoding(_legacy.read_bytes()), "BASELINE")
check("repair is idempotent", imagemod.repair_progressive(config.image_dir), 0)

print("\n[A2c] images must be 3-component colour, not 1-component greyscale")
# A greyscale JPEG carries a single colour component. Plenty of small readers
# (microcontroller e-readers especially) only implement 3-component YCbCr and
# render nothing at all, with no error anywhere to show for it.
_grey = config.image_dir / "legacy-greyscale.jpg"
_Image.new("L", (500, 300), 128).save(_grey, "JPEG", quality=85)
check("fixture really is single-component",
      imagemod.jpeg_components(_grey.read_bytes()), 1)
_n = imagemod.repair_greyscale(config.image_dir)
truthy("repair reported work", _n >= 1)
check("cached image now 3-component",
      imagemod.jpeg_components(_grey.read_bytes()), 3)
check("repair is idempotent", imagemod.repair_greyscale(config.image_dir), 0)

# And the writer itself, with greyscale off.
_srv2 = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_srv2.serve_forever, daemon=True).start()
_colour = imagemod.download(
    f"http://127.0.0.1:{_srv2.server_port}/hero2.jpg", config.image_dir,
    user_agent="test", timeout=10, max_width=1200, max_height=1600,
    quality=80, grayscale=False)
_srv2.shutdown()
if _colour:
    check("writer produces 3-component when greyscale is off",
          imagemod.jpeg_components((config.image_dir / _colour).read_bytes()), 3)

print("\n[A3] a page's og:image is preferred over a feed thumbnail")
from app.pipeline import extract as extractmod  # noqa: E402

PAGE = """<html><head>
<meta property="og:image" content="/media/large-hero.jpg">
<meta name="twitter:image" content="https://example.com/other.jpg">
</head><body><p>text</p></body></html>"""
check("og:image found and made absolute",
      extractmod.hero_from_page(PAGE, "https://example.com/news/story"),
      "https://example.com/media/large-hero.jpg")
check("twitter:image used when og is absent",
      extractmod.hero_from_page(
          '<html><head><meta name="twitter:image" content="https://e.com/t.jpg">'
          "</head><body></body></html>", "https://e.com/a"),
      "https://e.com/t.jpg")
check("no image tag means no hero",
      extractmod.hero_from_page("<html><body><p>x</p></body></html>",
                                "https://e.com/a"), None)
check("data: URIs rejected",
      extractmod.hero_from_page(
          '<html><head><meta property="og:image" content="data:image/gif;base64,R0lG">'
          "</head><body></body></html>", "https://e.com/a"), None)

print("\n[B] RSS parsing of a realistic feed")
SAMPLE = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>Example</title>
<item>
  <title>A Story</title>
  <link>https://example.com/a</link>
  <guid>https://example.com/a</guid>
  <pubDate>Mon, 08 Sep 2026 10:00:00 GMT</pubDate>
  <author>reporter@example.com (A Reporter)</author>
  <description><![CDATA[<p>Teaser text.</p>]]></description>
  <media:thumbnail url="https://example.com/pic.jpg"/>
</item>
</channel></rss>"""
parsed = feedparser.parse(SAMPLE)
check("one entry", len(parsed.entries), 1)
entry = parsed.entries[0]
check("guid", entry.get("id"), "https://example.com/a")
check("image picked", _pick_image(entry), "https://example.com/pic.jpg")
truthy("date parsed", _to_dt(entry.get("published_parsed")))
summary, content = _entry_html(entry)
truthy("summary html", "Teaser text" in (summary or ""))

print("\n[C] sanitiser strips furniture, keeps prose")
DIRTY = """
<div class="article">
  <script>track();</script>
  <nav><a href="/x">Home</a></nav>
  <div class="social-share"><a href="#">Share on X</a></div>
  <p>The <a href="https://tracker.example/?utm=1">real sentence</a> survives.</p>
  <p style="color:red" onclick="evil()">Second paragraph.</p>
  <img src="https://example.com/beacon.gif" width="1" height="1">
  <figure><img src="https://example.com/real.jpg" alt="A photo"></figure>
  <div class="newsletter-signup">Subscribe now</div>
  <p></p>
</div>"""
out = clean.sanitize(DIRTY)
print(f"        {out!r}")
truthy("script gone", "track()" not in out)
truthy("nav gone", "Home" not in out)
truthy("share block gone", "Share on X" not in out)
truthy("newsletter gone", "Subscribe now" not in out)
truthy("prose kept", "real sentence survives" in out)
truthy("second para kept", "Second paragraph." in out)
truthy("link text kept, href dropped", "tracker.example" not in out)
truthy("inline style dropped", "color:red" not in out)
truthy("onclick dropped", "onclick" not in out)
truthy("beacon gone", "beacon.gif" not in out)
truthy("real image kept", "real.jpg" in out)
truthy("alt kept", 'alt="A photo"' in out)
truthy("empty p dropped", "<p></p>" not in out)

print("\n[C2] decorative symbols are simplified, real typography is not")
# A FitGirl digest ended "sorted by date ➧➧➧" and the reader drew three empty
# boxes: U+27A7 is a dingbat, and small e-ink fonts carry no glyph for it.

# What must be replaced or dropped.
check("dingbat arrow becomes ascii",
      clean.simplify_symbols_text("sorted by date ➧➧➧"),
      "sorted by date >>>")
check("plain arrows", clean.simplify_symbols_text("a → b ← c"),
      "a -> b <- c")
check("check marks", clean.simplify_symbols_text("✓ done"), "[x] done")
check("stars", clean.simplify_symbols_text("★★"), "**")
check("an unmapped dingbat is dropped, not left as a box",
      clean.simplify_symbols_text("x ❖ y"), "x y")
check("emoji are dropped",
      clean.simplify_symbols_text("nice \U0001F600 one"), "nice one")

# What must survive untouched -- this is the half that matters most.
for label, sample in [
    ("en dash", "Cooking Simulator – v7.5.0"),
    ("em dash", "a — b"),
    ("curly quotes", "‘single’ and “double”"),
    ("ellipsis", "wait…"),
    ("bullet", "• item"),
    ("accents", "sença fácil, Noël Godin"),
    ("cyrillic", "Привет"),
    ("cjk", "日本語"),
    ("currency", "£26m and €5"),
]:
    check(f"{label} untouched", clean.simplify_symbols_text(sample), sample)

# Over HTML, leaving verbatim blocks alone.
_html = "<p>date ➧➧➧</p><pre>arrow ➧ in code</pre>"
_out = clean.simplify_symbols(_html)
# ">" is serialised as &gt;, which is correct HTML, so assert on the rendered
# text rather than the raw markup.
truthy("html body simplified", "date >>>" in clean.text_of(_out))
truthy("escaped correctly in the markup", "&gt;&gt;&gt;" in _out)
truthy("pre block left verbatim", "➧" in _out)
truthy("markup preserved", "<pre>" in _out and "<p>" in _out)

print("\n[D] the AI retention guard")
long_html = "<p>" + " ".join(f"Sentence number {i}." for i in range(200)) + "</p>"


def fake_summariser(*_a, **_k):
    return "<p>The article was about some sentences.</p>"


original = clean._ollama_generate
clean._ollama_generate = fake_summariser
body, by = clean.ai_clean(long_html, base_url="http://x", model="m",
                          timeout=5, num_ctx=8192, min_retain=0.55)
clean._ollama_generate = original
check("summarising model rejected", by, "rules")
truthy("full text retained", "Sentence number 199." in body)


def fake_faithful(*_a, **_k):
    return long_html


clean._ollama_generate = fake_faithful
body2, by2 = clean.ai_clean(long_html, base_url="http://x", model="m",
                            timeout=5, num_ctx=8192, min_retain=0.55)
clean._ollama_generate = original
check("faithful model accepted", by2, "m")


def fake_down(*_a, **_k):
    raise OSError("connection refused")


import httpx  # noqa: E402
clean._ollama_generate = lambda *a, **k: (_ for _ in ()).throw(
    httpx.ConnectError("refused"))
body3, by3 = clean.ai_clean(long_html, base_url="http://x", model="m",
                            timeout=5, num_ctx=8192, min_retain=0.55)
clean._ollama_generate = original
check("ollama offline falls back", by3, "rules")
truthy("text still intact when ollama is down",
       "Sentence number 199." in body3)

print("\n[D2] ollama requests carry keep_alive and never overlap")
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402

_seen_payloads = []
_concurrent = {"now": 0, "peak": 0}
_lock = _threading.Lock()


class _FakeResponse:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"response": self._text}


def _fake_post(url, json=None, timeout=None, **kw):
    with _lock:
        _concurrent["now"] += 1
        _concurrent["peak"] = max(_concurrent["peak"], _concurrent["now"])
    _seen_payloads.append(json)
    _time.sleep(0.05)          # long enough for an overlap to be observable
    with _lock:
        _concurrent["now"] -= 1
    return _FakeResponse(json["prompt"].split("---", 1)[-1])


_real_post = clean.httpx.post
clean.httpx.post = _fake_post
try:
    body = "<p>" + " ".join(f"Sentence {i}." for i in range(120)) + "</p>"
    threads = [_threading.Thread(target=clean.ai_clean, args=(body,),
                                 kwargs=dict(base_url="http://x", model="m",
                                             timeout=5, num_ctx=8192,
                                             min_retain=0.55,
                                             keep_alive="45m"))
               for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
finally:
    clean.httpx.post = _real_post

truthy("requests were actually made", len(_seen_payloads) > 0)
check("keep_alive is sent", _seen_payloads[0].get("keep_alive"), "45m")
check("streaming stays off", _seen_payloads[0].get("stream"), False)
check("never more than one request in flight", _concurrent["peak"], 1)
print(f"        {len(_seen_payloads)} request(s) from 4 threads, peak "
      f"concurrency {_concurrent['peak']}")

print("\n[D3] configurable word threshold, prompt, and the OpenAI-compatible backend")
_short = "<p>" + " ".join(f"word{i}" for i in range(10)) + "</p>"

body, by = clean.ai_clean(_short, base_url="http://x", model="m", timeout=5,
                          num_ctx=8192, min_retain=0.55)
check("a 10-word article skips AI at the default threshold", by, "rules")

_calls = []
clean._ollama_generate = lambda *a, **k: (_calls.append(a) or _short)
body, by = clean.ai_clean(_short, base_url="http://x", model="m", timeout=5,
                          num_ctx=8192, min_retain=0.55, min_words=2)
clean._ollama_generate = original
check("a lowered min_words setting sends it to the model", by, "m")
truthy("exactly one call made", len(_calls) == 1)

_prompts = []


def _capture_prompt(base_url, model, prompt, timeout, num_ctx, keep_alive="30m"):
    _prompts.append(prompt)
    return _short


clean._ollama_generate = _capture_prompt
clean.ai_clean(_short, base_url="http://x", model="m", timeout=5, num_ctx=8192,
               min_retain=0.55, min_words=2, prompt="CUSTOM {chunk} END")
clean._ollama_generate = original
truthy("a custom prompt template is actually sent",
       _prompts and _prompts[0].startswith("CUSTOM") and _prompts[0].endswith("END"))

_calls.clear()
clean._ollama_generate = lambda *a, **k: (_calls.append(a) or _short)
body, by = clean.ai_clean(_short, base_url="http://x", model="m", timeout=5,
                          num_ctx=8192, min_retain=0.55, min_words=2,
                          prompt="no placeholder here")
clean._ollama_generate = original
check("a prompt missing {chunk} is refused, not sent", by, "rules")
truthy("and no request was made for it", len(_calls) == 0)

_openai_calls = []


def _fake_openai(base_url, model, prompt, timeout, api_key=""):
    _openai_calls.append((base_url, model, prompt, api_key))
    return _short


original_openai = clean._openai_generate
clean._openai_generate = _fake_openai
body, by = clean.ai_clean(_short, base_url="http://x/v1", model="gpt", timeout=5,
                          num_ctx=8192, min_retain=0.55, min_words=2,
                          backend="openai", api_key="sk-test")
clean._openai_generate = original_openai
check("openai backend is used when selected", by, "gpt")
truthy("the api key is carried through",
       _openai_calls and _openai_calls[0][3] == "sk-test")


class _FakeModelsResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_models_get(url, headers=None, timeout=None, **kw):
    if url.endswith("/api/tags"):
        return _FakeModelsResponse({"models": [{"name": "b"}, {"name": "a"}]})
    check("openai model listing sends the bearer token",
          (headers or {}).get("Authorization"), "Bearer sk-x")
    return _FakeModelsResponse({"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})


_real_get = clean.httpx.get
clean.httpx.get = _fake_models_get
check("ollama model list, sorted", clean.list_models("ollama", "http://x:11434"),
      ["a", "b"])
check("openai model list, sorted", clean.list_models("openai", "http://x/v1", api_key="sk-x"),
      ["gpt-4o", "gpt-4o-mini"])
clean.httpx.get = _real_get

print("\n[E] title derivation and thread markers")
check("short post title",
      derive_title("<p>Hello there.</p>"), "Hello there.")
truthy("long post truncated",
       derive_title("<p>" + "word " * 60 + "</p>").endswith("…"))
check("marker stripped from title",
      derive_title("<p>Big news 1/5</p>"), "Big news")
truthy("code block markers preserved",
       "1/2" in clean.strip_thread_markers("<pre>ratio 1/2</pre>"))

print("\n[G] the hero image actually reaches the book")
from app.pipeline import epub as epubmod  # noqa: E402


class _FakeFeed:
    title = "A Feed"


class _FakeArticle:
    """An article whose only image lives on image_file, as RSS ones do."""
    title = "Has a hero"
    byline = None
    feed = _FakeFeed()
    published_at = None
    word_count = 10
    part_count = 1
    body_html = "<p>Body with no inline image at all.</p>"
    image_file = "hero.jpg"


chapter_html = epubmod._chapter_html(_FakeArticle(), 1)
# Stored files are hash-named; inside the book they become img1.jpg etc.
truthy("hero rendered into the chapter",
       'src="images/img1.jpg"' in chapter_html)
check("hero is planned for embedding",
      epubmod._plan_images(_FakeArticle()), {"hero.jpg": "img1.jpg"})

_FakeArticle.body_html = '<p>x</p><img src="images/hero.jpg"/>'
check("hero not duplicated when already inline",
      epubmod._chapter_html(_FakeArticle(), 1).count("images/img1.jpg"), 1)

# A body image that is not the hero still gets its own slot.
class _TwoImageArticle(_FakeArticle):
    body_html = '<p>x</p><img src="images/other.jpg"/>'
    image_file = "hero.jpg"


check("hero first, then body images",
      epubmod._plan_images(_TwoImageArticle()),
      {"hero.jpg": "img1.jpg", "other.jpg": "img2.jpg"})
_two = epubmod._chapter_html(_TwoImageArticle(), 1)
truthy("both images rewritten",
       'src="images/img1.jpg"' in _two and 'src="images/img2.jpg"' in _two)

print("\n[F] byte coverage maths")
from app.pipeline.editions import covered_bytes, merge_intervals  # noqa: E402
check("overlapping merge", merge_intervals([(0, 50), (40, 100)]), [(0, 100)])
check("gap preserved", merge_intervals([(0, 10), (20, 30)]),
      [(0, 10), (20, 30)])
check("out of order", merge_intervals([(50, 60), (0, 10)]),
      [(0, 10), (50, 60)])
check("coverage with gap", covered_bytes([(0, 10), (20, 30)]), 20)
check("coverage overlapping", covered_bytes([(0, 50), (25, 75)]), 75)

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
