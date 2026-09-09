# RSSOPDS

Pulls RSS/Atom feeds and Threads (Meta) posts, cleans them with a local model,
builds one EPUB per category, and serves them over OPDS. Articles disappear
from the catalogue once the ereader has actually finished downloading them.

## Running it

```sh
docker compose up -d --build
```

Web UI and OPDS on port 8000:

- `http://<host>:8000/` — configuration and status
- `http://<host>:8000/opds` — point the ereader here

Verified on Ubuntu 26.04 / Docker 29.7.2 (`LinuxCodeTesting`, 192.168.10.189),
built from this Dockerfile and driven end to end against a live BBC RSS feed.

Ollama is expected on the host, not in the container. Set the **Ollama base
URL** in Settings (default `http://host.docker.internal:11434`) and pull a
model, e.g. `ollama pull qwen2.5:7b-instruct`.

Everything else is configured in the web UI. Only paths, the bind address and
the credentials come from the environment, because they are needed before the
database opens.

## Authentication

HTTP basic auth, off until you configure it. Credentials live in `.env`, never
in the database, so they are never served back out through the web UI:

```sh
cp .env.example .env      # then edit it
docker compose up -d      # read at startup only, so restart after editing
```

| Variable | Effect |
| --- | --- |
| `RSSOPDS_WEB_USER` / `_PASSWORD` | Protects the whole web UI. Blank user = no auth at all. |
| `RSSOPDS_OPDS_USER` / `_PASSWORD` | A separate credential for the ereader. Blank = the OPDS catalogue reuses the web pair. |
| `RSSOPDS_OPDS_PUBLIC` | `true` leaves OPDS open while the web UI stays protected — for readers that cannot send credentials. |

Setting only the web pair protects **both** the UI and OPDS. Give the device
its own shorter credential if you would rather not type a long one on a device
keyboard.

`/healthz` is deliberately left open so the container healthcheck works; it
exposes a feed count and scheduler status, nothing else.

`.env` is gitignored. Note that basic auth over plain HTTP sends credentials
in reversible base64 — fine on a home LAN, not something to expose to the
internet without TLS in front.

## How a story travels

```
Feed  --poll-->  FeedItem  --assemble-->  Article  --process-->  Article
                  (raw)        (N:1)      (pending)   (clean,     (ready)
                                                      images)
                                                                    |
                                        Edition  <----build---------+
                                       (one EPUB
                                      per category)
                                            |
                                     OPDS download
                                            |
                                    all bytes arrived?
                                       |          |
                                      yes         no
                                       |          |
                                  delivered    stays in
                                 (never shown   catalogue
                                    again)
```

The two-level split between `FeedItem` and `Article` is the load-bearing part.
Read state lives on `Article`, because a Threads self-thread is several feed
GUIDs but exactly one thing the reader should see once.

## Threads (Meta)

Threads has no RSS feed, and Meta's Threads API only reads *your own* account.
To follow other people this app uses ActivityPub:

1. The Threads user must have **Fediverse sharing** enabled in their Threads
   settings. Without it their posts are not visible outside Threads at all.
2. You need a bot account on any Mastodon instance and an application token
   with `read` scope.
3. That account follows the handles you want (`someone@threads.net`).
4. Those follows go in a Mastodon list, and a feed of kind **Fediverse** polls
   `GET /api/v1/timelines/list/:id`.

The **Threads** tab in the web UI does steps 2–4: verify a token, create a
list, and follow a handle.

Two things that are not bugs:

- **No backfill.** You only receive posts made after the follow, so a newly
  added account looks empty at first.
- **Partial conversations.** Replies from non-federated accounts never
  federate, so you see one side only.

Alternatives considered: the official Threads API (own account only) and
scraper bridges (break constantly, against Meta's ToS). A bridge URL will still
work if you want one — it is just an RSS feed as far as this app is concerned.

### Self-threads

With **Merge self-threads** on, a chain of self-replies becomes one article.
The Mastodon API gives `in_reply_to_account_id`, so this is exact rather than
guessed from "1/n" markers or timing.

A thread is **held open** for `thread_grace_minutes` (default 3h) after its
newest part. This is the setting that matters: publish immediately and parts
1–3 ship to the ereader, get marked read, vanish, and parts 4–6 turn up the
next day as an orphan with no beginning. Held threads are visible on the
Articles page and can be released early.

Replies to *other* accounts are dropped in this mode — without the other half
of the conversation they read as non sequiturs. Use **One post = one article**
on a feed where you want every reply.

## Cleaning

Rule-based sanitising always runs: scripts, nav, share widgets, newsletter
prompts, tracking beacons and all attributes go; link text is kept but hrefs
are dropped, since links are dead weight offline.

The Ollama pass is optional polish on top and is never trusted blind — if the
model returns less than `ai_min_retain_ratio` (default 55%) of the text it was
given, its answer is discarded and the rule-based output is used. A model
quietly summarising an article it was asked to tidy is the most likely way this
pipeline could corrupt your reading, so the guard is not optional. If Ollama is
down, articles still process.

## EPUB layout

The books are EPUB 2, written by hand in `pipeline/epub.py` rather than with a
library. That is a deliberate compatibility choice, not a preference.

A CrossPoint-ESP32 reader showed the text of the ebooklib-built books but none
of their images, across ten variants covering format, pixel size, colour mode,
path shape and container element — while rendering images fine from a
calibre-built book. The calibre file ships *progressive* JPEGs at 2560x1440 and
1.2MB, which rules out encoding, dimensions and colour mode. The difference was
the package:

| | calibre (works) | ebooklib |
| --- | --- | --- |
| version | `2.0` | `3.0` |
| OPF | at the zip root | `EPUB/content.opf` |
| nav | `toc.ncx` only | `nav.xhtml` + `toc.ncx` |
| content | `feed_x/article_y/` beside the OPF | all under `EPUB/` |
| names | short `.html` | long `.xhtml` |

So the writer reproduces the calibre layout. EPUB 2 with an NCX is also the
more conservative choice generally — every reader understands it, including
ones far older than EPUB 3.

Images are copied into `article_NNN/images/imgN.jpg`, renamed from their stored
hashes so paths stay short. `tools/packagingtest.py` builds books in several
different layouts if you ever need to work out what a new reader accepts.

## Images

Two things about images are not optional if you want them to appear on an
e-reader:

**Baseline JPEG, never progressive.** Progressive JPEGs decode fine in every
browser and render as *nothing* on Adobe RMSDK devices — Kobo, Nook, Sony —
with the image still present in the book and correctly referenced, so nothing
looks wrong anywhere except the screen. `images.py` writes baseline only, and
`repair_progressive()` re-encodes anything already cached at startup, then
forces undelivered editions to rebuild so the fixed bytes actually ship.

**Take the page's image, not the feed's.** Feed thumbnails are small — BBC's
`media:thumbnail` is 240px wide, a postage stamp on a 1400px screen. The
article page is already being fetched for its text, so its `og:image` /
`twitter:image` is read from the same response. On a real BBC article that is
the difference between 240×135 and 1200×675.

Hero images are picked in order: an image inside the article body, then the
page's `og:image`, then the feed thumbnail. Everything is downscaled to
`image_max_width` and converted to greyscale by default, since e-ink is
greyscale anyway and it roughly halves the file.

## Delivery detection

OPDS has no "the download worked" callback. The only evidence is bytes the
server handed to the network, and readers like KOReader use Range requests --
so a single 206 proves nothing. Each `Delivery` accumulates **merged byte intervals**
across every request from that client, and the edition counts as delivered only
once the union covers `delivery_min_fraction` (default 98%) of the file.

After that a `delivery_confirm_delay_s` grace period runs before the articles
are marked read, so a sync that completed the transfer but failed on the device
can still be undone from the Editions page.

A category with no available edition is simply absent from the OPDS feed, which
is what makes empty sections disappear from the ereader.

**Known limit.** "Bytes sent" means bytes handed to the transport, not a
receipt from the device — HTTP offers no such receipt. A response small enough
to fit in one kernel socket buffer (roughly a few hundred KB) is accepted
whole, so a reader that connects and dies immediately still reads as a full
transfer. Past that size asyncio flow control makes the count track real
progress. Editions with images clear that bar; a tiny text-only one does not.
If a sync looks successful but the device has nothing, use **Mark unread** on
the Editions page.

## Edition naming

Ereaders name the saved file from the OPDS metadata, roughly
`<author> - <title>.epub`. So a title with only day granularity —
`Technology - 08 Sep 2026` — produces the same filename for every edition
built that day, and each new download silently overwrites the last one on the
device.

Each edition therefore gets a per-category issue number, and all three places
a reader might take a name from are distinct per edition:

| Source | Example |
| --- | --- |
| OPDS author | `Technology` (the category) |
| OPDS title | `No. 9 - 08 Sep 2026` |
| URL | `/opds/edition/9/technology-no009-20260908-1541.epub` |
| `Content-Disposition` | `technology-no009-20260908-1541.epub` |

A reader that names files `<author> - <title>.epub` therefore saves
`Technology - No. 9 - 08 Sep 2026.epub`.

`/opds/edition/<id>.epub` still works, so an existing catalogue entry on a
device keeps functioning.

The issue number never repeats for a category, including across superseded and
deleted editions. `edition_title_format` must contain `{n}`, `{time}` or
`{datetime}`; saving one without any of them warns you on the Settings page.

## Naming and branding

Nothing this application is called reaches the catalogue, the EPUB metadata or
the saved filename. The author written into every book and catalogue entry is
the **category** by default, so an ereader library groups editions by category
the way it would group a magazine by title. Set **Author / publisher name** in
Settings to override it with your own.

`catalog_title` (default `Library`) names the catalogue on the device, and the
outgoing `http_user_agent` is a plain `FeedReader/1.0`. The web UI keeps its
own name — it is yours, not the device's.

## Upgrading

`init_db()` adds any model columns missing from an existing SQLite file, so a
new release picks up schema changes on first boot without Alembic. It also
backfills issue numbers for editions created before numbering existed, and
moves a setting still holding a superseded default forward to the new one — a
value you customised yourself is never touched. That covers the edition title
format, the catalogue title and the HTTP user agent, all of which once carried
this application's name.

## OPDS log

`/opds-log` shows every request to `/opds` — which device, what it asked for,
the status, how many bytes moved and any `Range` header — plus a summary of
clients seen in the last week. It refreshes every 3 seconds, pauses when the
tab is hidden, and is the quickest way to answer "is the ereader actually
talking to this thing?". 401s and 404s show up here too, which is usually what
you need when a reader will not connect.

Only real `/opds` traffic is recorded; the log page and its own poller are
excluded, so the list stays quiet when nothing is happening. Retention is
`opds_log_keep` (default 1000 rows).

## Tests

```sh
python tests/test_pipeline.py   # thread merge -> epub -> OPDS -> ranged download
python tests/test_output.py     # EPUB structure, RSS parsing, cleaner guards
```

Both are self-contained and hit no network. Point `RSSOPDS_DATA_DIR` at a
scratch directory first, or they will write into `./data`. To run them against
the built image:

```sh
docker cp tests rssopds:/app/tests
docker exec -e RSSOPDS_DATA_DIR=/tmp/t rssopds python /app/tests/test_pipeline.py
```

(`docker cp` nests into an existing directory — `docker exec rssopds rm -rf
/app/tests` first when re-copying.)

## Layout

| Path | What it is |
| --- | --- |
| `app/models.py` | Schema. Start here. |
| `app/sources/` | `rss.py`, `fediverse.py` (Threads) behind one adapter contract |
| `app/pipeline/assemble.py` | Feed items → articles, incl. self-thread merging |
| `app/pipeline/clean.py` | Sanitiser + guarded Ollama pass |
| `app/pipeline/editions.py` | EPUB building, byte coverage, delivery state |
| `app/opds/` | Catalogue XML, the tracking download endpoint, access log |
| `app/web/` | Configuration UI |
| `app/scheduler.py` | APScheduler wiring; reloads on any settings change |
