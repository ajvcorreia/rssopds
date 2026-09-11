# RSSOPDS

Pulls RSS/Atom feeds and Threads (Meta) posts, cleans the articles with a
local LLM, builds one EPUB per category, and serves them over OPDS so an
e-reader can sync them like a magazine subscription. Articles disappear from
the catalogue once the reader has actually finished downloading them.

## Features

- **RSS/Atom and Threads (Meta)** as sources, the latter over ActivityPub —
  see [Threads (Meta)](#threads-meta) below.
- **Self-thread merging**: a chain of replies from the same author becomes one
  article instead of several fragments.
- **AI cleanup with a safety net**: an optional local-LLM pass tidies each
  article, but its output is discarded automatically if it drops too much of
  the original text — the rule-based cleaner always runs first as a floor.
- **One EPUB per category**, rebuilt automatically as new articles arrive.
- **Delivery-aware**: articles are marked read only once the e-reader has
  fully downloaded the edition, inferred from actual bytes transferred rather
  than a request succeeding.
- **A live OPDS access log** — see exactly which device connected, what it
  asked for, and whether the transfer completed.
- **Everything configured from the web UI** — feeds, categories, cleanup
  behaviour, cover art, image sizing, all editable without touching a file.
- **An Ebooks folder alongside RSS**: drop your own books into `data/ebooks/`
  on the server (in whatever folders you like) and browse them from the same
  OPDS catalogue — see [Ebooks](#ebooks) below.

## Quick start

```sh
cp .env.example .env      # optional — see Authentication below
docker compose pull
docker compose up -d
```

That pulls the image published to Docker Hub by CI on every push to `main`.
Building from source instead — for local development, or to pick up
uncommitted changes — works the same way it always has:

```sh
docker compose up -d --build
```

Then open:

- `http://<host>:8000/` — the web UI: add feeds, categories, tune settings
- `http://<host>:8000/opds` — point your e-reader's OPDS client here

If you want the AI cleanup pass, run [Ollama](https://ollama.com) somewhere
reachable from the container and set **Ollama base URL** in Settings (default
`http://host.docker.internal:11434`), then pull a model, e.g.
`ollama pull qwen2.5:7b-instruct` — or point Settings → AI at any
OpenAI-compatible endpoint instead (see [Cleaning](#cleaning) below).
Everything else — feeds, categories, image sizing, cleanup behaviour — is
configured from the web UI, not files.

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
| `RSSOPDS_OPDS_USER` / `_PASSWORD` | A separate credential for the e-reader. Blank = the OPDS catalogue reuses the web pair. |
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
1–3 ship to the e-reader, get marked read, vanish, and parts 4–6 turn up the
next day as an orphan with no beginning. Held threads are visible on the
Articles page and can be released early.

Replies to *other* accounts are dropped in this mode — without the other half
of the conversation they read as non sequiturs. Use **One post = one article**
on a feed where you want every reply.

## Cleaning

Rule-based sanitising always runs: scripts, nav, share widgets, newsletter
prompts, tracking beacons and all attributes go; link text is kept but hrefs
are dropped, since links are dead weight offline.

The AI pass on top is optional polish, and is never trusted blind — if the
model returns less than `ai_min_retain_ratio` (default 55%) of the text it was
given, its answer is discarded and the rule-based output is used. A model
quietly summarising an article it was asked to tidy is the most likely way this
pipeline could corrupt your reading, so the guard is not optional. Articles
under `ai_min_words` (default 40 — most Threads posts) skip the model
round trip entirely. If the model is unreachable, articles still process using
the rule-based output.

**Backend**: Settings → AI lets you point cleanup at either a local
[Ollama](https://ollama.com) server or any OpenAI-compatible
chat-completions endpoint (vLLM, LM Studio, llama.cpp server,
text-generation-webui, OpenAI itself, ...). Enter the base URL (and API key,
for OpenAI-compatible) and click **Fetch available models** to list what the
server actually has, rather than typing a model name blind. Once a model is
picked, **Test model** sends it one trivial request through the same code
path real cleanup uses, so a wrong model name, a bad API key or an
unreachable server shows up immediately, with the model's actual reply
displayed, instead of only being discovered on the next real article. Only
one request is in flight at a time regardless of backend — a local model has
no spare capacity for concurrent generation, and a remote one's rate limits
aren't known up front.

**Prompt**: the instructions sent to the model, together with the article
HTML, are fully editable from Settings → AI. The prompt must contain the
literal text `{chunk}` — that's where the article gets inserted — a save
that removes it is rejected rather than silently sending the model nothing.
A **Reset to default prompt** button restores the built-in wording.

## EPUB layout

The books are EPUB 2, written by hand in `app/pipeline/epub.py` rather than
with a packaging library: the OPF sits at the zip root, navigation is a plain
NCX with no `nav.xhtml`, and each article gets a short-named folder
(`article_000/index.html`) rather than being flattened into one file. That
combination is deliberately the most conservative shape available — it is
understood by essentially every EPUB reader ever built, including ones that
only partially implement EPUB 3. Contents are grouped under a heading per
source feed, both on the contents page and in the two-level NCX, so a device's
own table-of-contents menu groups the same way.

Images are copied into `article_NNN/images/imgN.jpg`, renamed from their
stored hashes so paths stay short. `tools/packagingtest.py` builds books in
several different layouts if you need to work out what a particular reader
accepts.

## Images

Two compatibility choices, on by default:

**Baseline JPEG, never progressive.** Progressive JPEGs decode fine in modern
browsers but render as *nothing* on a number of e-reader devices — the image
is present and correctly referenced in the book, so nothing looks wrong
anywhere except the screen. `app/pipeline/images.py` writes baseline only, and
a startup repair pass re-encodes anything already cached, then forces
undelivered editions to rebuild so the fixed bytes actually ship.

**3-component colour JPEG by default, not single-component greyscale**, for
the same class of reason — a single-component (greyscale) JPEG is a real
compatibility risk on some small decoders, even though it is smaller. Turn
`image_grayscale` on in Settings if you have confirmed your own reader copes
with it; e-ink displays it identically to colour anyway.

**Take the page's image, not the feed's.** Feed thumbnails are often tiny —
some RSS `media:thumbnail` entries are only a couple hundred pixels wide,
noticeably worse than an e-reader screen. The article page is already being
fetched for its text, so its `og:image` / `twitter:image` is read from the
same response instead.

Hero images are picked in order: an image inside the article body, then the
page's `og:image`, then the feed thumbnail. Everything is downscaled to fit
`image_max_width` × `image_max_height` — set these to your reader's actual
screen resolution in Settings.

## Delivery detection

OPDS has no "the download worked" callback. The only evidence is bytes the
server handed to the network, and readers commonly use Range requests — so a
single 206 proves nothing on its own. Each `Delivery` accumulates **merged
byte intervals** across every request from that client, and the edition
counts as delivered only once the union covers `delivery_min_fraction`
(default 98%) of the file.

After that a `delivery_confirm_delay_s` grace period runs before the articles
are marked read, so a sync that completed the transfer but failed on the
device can still be undone from the Editions page.

A category with no available edition is simply absent from the OPDS feed,
which is what makes empty sections disappear from the reader.

An edition the reader has already fully downloaded is never superseded by a
later rebuild — it is confirmed as delivered instead. Superseding an already
downloaded edition would return its articles to the pool and re-serve them as
unread in the next book.

**Known limit.** "Bytes sent" means bytes handed to the transport, not a
receipt from the device — HTTP offers no such receipt. A response small
enough to fit in one kernel socket buffer (roughly a few hundred KB) is
accepted whole, so a reader that connects and disconnects immediately can
still read as a full transfer. Past that size, flow control makes the count
track real progress. Editions with images comfortably clear that bar; a tiny
text-only one may not. If a sync looks successful but the device has nothing,
use **Mark unread** on the Editions page.

## Edition naming

E-readers commonly name the saved file from the OPDS metadata, roughly
`<author> - <title>.epub`. A title with only day granularity —
`Technology - 08 Sep 2026` — would produce the same filename for every
edition built that day, silently overwriting the previous download.

Each edition gets a per-category issue number, and all three places a reader
might take a name from are distinct per edition: the OPDS `<title>`, the
download URL, and the `Content-Disposition` filename all include it, e.g.
`Technology No. 9 - 08 Sep 2026`. `/opds/edition/<id>.epub` (without the
issue-numbered name) still works too, so a catalogue entry already saved on a
device keeps functioning.

The issue number only advances once an edition has actually been delivered —
by a real download completing, or an admin "Mark read". A rebuild that
replaces an edition nobody has downloaded yet reuses its number instead of
incrementing, so a reader that has not synced in a while does not see the
number climb for content it was never offered. Once a number has been
delivered it is never reused again. `edition_title_format` must contain
`{n}`, `{time}` or `{datetime}`; saving one without any of them warns you on
the Settings page.

## Naming and branding

Nothing this application is called reaches the catalogue, the EPUB metadata,
or the saved filename. The author written into every book and catalogue entry
is the **category** by default, so an e-reader library groups editions by
category the way it would group a magazine by title. Set **Author / publisher
name** in Settings to override it with your own.

`catalog_title` (default `Library`) names the catalogue on the device.

## Upgrading

`init_db()` adds any model columns missing from an existing SQLite file on
startup, so a new release picks up schema changes without a separate
migration step. It also moves a setting still holding a superseded default
forward to the new one — a value you customised yourself is never touched.

## Ebooks

`/opds` is a small folder view with two entries: **RSS** (everything described
above) and **Ebooks** — a plain, read-only mirror of whatever sits in
`data/ebooks/` on the server. Populate it either by dropping files in over
whatever file-sharing method you already use (`scp`, a network share,
`docker cp`, ...), or from the **Ebooks** page in the web UI, which can
upload files, create folders, and delete entries directly — no server access
needed. Either way the OPDS catalogue reflects the folder structure as-is, no
configuration needed, no database involved.

This is deliberately dumb: no AI cleaning, no cover extraction, no
read-tracking or disappearing-once-read behaviour like the RSS side has —
just folders and downloadable files, browsable from any OPDS client. Any file
type is served (with a best-effort content type from its extension); nothing
is validated as an actual ebook.

Because an ereader that only understands a flat acquisition feed will not
show folders, point such a device directly at `/opds/rss` to skip straight to
the RSS side, exactly as before this feature existed.

## OPDS log

`/opds-log` shows every request to `/opds` — which device, what it asked for,
the status, how many bytes moved, and any `Range` header — plus a summary of
clients seen in the last week. It refreshes every few seconds, pauses when the
tab is hidden, and is the quickest way to answer "is the e-reader actually
talking to this thing?". 401s and 404s show up here too, which is usually what
you need when a reader will not connect.

Retention is `opds_log_keep` (default 1000 rows).

## Backup & restore

Settings has a **Backup & Restore** card. A backup is one `.tar.gz`: a
consistent snapshot of the database (taken via SQLite's own backup API, safe
against a live database being written to concurrently, rather than a raw
file copy that could grab a torn write) plus every cached EPUB, image and
cover. The same file serves two purposes: protecting against data loss, and
moving the whole installation to a new machine -- restoring it there brings
across every feed, category, article and read-state, not just settings.

It does **not** include `.env` -- credentials are server-local by design and
set up separately on each machine. It **does** include anything stored in
the database, including a fediverse access token on a Threads feed, so treat
a backup file the way you would treat that token.

Restoring is destructive but not permanent: the current database and files
are moved aside into a timestamped folder rather than deleted, and an
uploaded file is fully validated -- checked as a real archive, checked for
path-traversal or symlink entries, and checked that its database has the
tables an RSSOPDS backup should have -- before anything about the running
installation is touched. Applying a valid restore ends the process a moment
after responding, so the container's restart policy brings it back up
against the restored files; give it about 15 seconds.

## Tests

```sh
python tests/test_pipeline.py   # thread merge -> epub -> OPDS -> ranged download
python tests/test_output.py     # EPUB structure, RSS parsing, cleaner guards
python tests/test_backup.py     # backup contents, restore round trip, path-traversal safety
```

Both are self-contained and hit no network. Point `RSSOPDS_DATA_DIR` at a
scratch directory first, or they will write into `./data`.

## Layout

| Path | What it is |
| --- | --- |
| `app/models.py` | Schema. Start here. |
| `app/sources/` | `rss.py`, `fediverse.py` (Threads) behind one adapter contract |
| `app/pipeline/assemble.py` | Feed items → articles, incl. self-thread merging |
| `app/pipeline/clean.py` | Sanitiser + guarded AI pass (Ollama or OpenAI-compatible) |
| `app/pipeline/epub.py` | Hand-written EPUB 2 writer |
| `app/pipeline/editions.py` | Edition building, byte coverage, delivery state |
| `app/opds/` | Catalogue XML, the tracking download endpoint, access log |
| `app/web/` | Configuration UI |
| `app/scheduler.py` | APScheduler wiring; reloads on any settings change |
| `app/backup.py` | Backup archive creation, restore validation and the file swap |
| `tools/` | Diagnostic scripts for EPUB compatibility and Ollama benchmarking |
