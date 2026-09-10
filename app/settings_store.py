"""Runtime settings, stored in the DB and edited entirely from the web UI."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Setting
from .pipeline.clean import PROMPT as _DEFAULT_AI_PROMPT

# key -> (default, type, group, label, help[, choices])
# choices is only present for kind "choice": a tuple of (value, display) pairs.
DEFAULTS: dict[str, tuple] = {
    "ai_backend": ("ollama", "choice", "AI", "Backend",
                  "Which server to send AI cleanup requests to.",
                  (("ollama", "Ollama"), ("openai", "OpenAI-compatible"))),
    "ollama_url": ("http://host.docker.internal:11434", "str", "AI",
                   "Ollama base URL",
                   "Where the local model server listens. Used when the "
                   "backend above is Ollama."),
    "ollama_model": ("qwen2.5:7b-instruct", "str", "AI", "Ollama model",
                     "Instruct model used to tidy article HTML. Use \"Fetch "
                     "available models\" to list what the server actually "
                     "has installed."),
    "openai_base_url": ("", "str", "AI", "OpenAI-compatible base URL",
                        "e.g. https://api.openai.com/v1, or any self-hosted "
                        "server exposing the OpenAI chat-completions API "
                        "(vLLM, LM Studio, llama.cpp server, "
                        "text-generation-webui, Ollama's own /v1 endpoint, "
                        "...). Used when the backend above is OpenAI-compatible."),
    "openai_api_key": ("", "str", "AI", "OpenAI-compatible API key",
                       "Sent as a Bearer token. Leave blank for a local "
                       "server with no auth."),
    "openai_model": ("", "str", "AI", "OpenAI-compatible model",
                     "Use \"Fetch available models\" to list what the "
                     "endpoint actually serves."),
    "ollama_timeout_s": (300, "int", "AI", "Request timeout (s)",
                         "Generous on purpose: the first request after a "
                         "local model is evicted has to load several GB from "
                         "disk, and that alone can exceed a short timeout. "
                         "Later requests are fast while the model stays "
                         "resident. Applies to either backend."),
    "ollama_keep_alive": ("30m", "str", "AI", "Keep the model loaded for",
                          "Passed to Ollama as keep_alive. Articles arrive "
                          "minutes apart, so without this the model is "
                          "unloaded between them and every single request "
                          "pays the full load cost -- which is what turns "
                          "into a timeout. Use 0 to unload immediately, or "
                          "-1 to keep it loaded indefinitely. Ollama backend "
                          "only."),
    "process_max_seconds": (240, "int", "AI", "Processing batch budget (s)",
                            "A processing run stops starting new articles "
                            "after this long and picks up again next time. "
                            "Without it, one slow model can hold the batch "
                            "open for the better part of an hour and every "
                            "later run is skipped, stalling the pipeline."),
    "ollama_num_ctx": (8192, "int", "AI", "Context window",
                       "Articles longer than this are cleaned in chunks. "
                       "Applies to either backend."),
    "ai_min_retain_ratio": (0.55, "float", "AI", "Minimum retained text ratio",
                            "If the model returns less than this fraction of the "
                            "original text, its output is discarded and the "
                            "rule-based clean is used instead. Guards against "
                            "the model summarising or truncating."),
    "ai_min_words": (40, "int", "AI", "Minimum words to bother with AI",
                     "Articles shorter than this skip the AI pass entirely "
                     "and go straight to the rule-based clean -- not worth a "
                     "model round trip. Most Threads posts land here."),
    "ai_clean_prompt": (_DEFAULT_AI_PROMPT, "text", "AI", "Cleanup prompt",
                        "Sent to the model with the article HTML in place of "
                        "{chunk}. Must contain the literal text {chunk} "
                        "somewhere, or the model never receives the article "
                        "and the AI pass is skipped."),

    "thread_grace_minutes": (180, "int", "Threads", "Self-thread hold-open (min)",
                             "A self-thread is not published until this long "
                             "after its most recent part, so a thread still "
                             "being written does not ship half-finished."),
    "thread_max_parts": (60, "int", "Threads", "Max parts per thread", ""),

    "build_min_articles": (1, "int", "Editions", "Minimum articles to build",
                           "Skip building an edition for a category with fewer "
                           "unread articles than this."),
    "build_max_articles": (80, "int", "Editions", "Maximum articles per edition", ""),
    "edition_title_format": ("No. {n} - {date}", "str", "Editions",
                             "Edition title",
                             "Placeholders: {category} {n} {date} {time} "
                             "{datetime} {count}. Ereaders name the downloaded "
                             "file after this title, so it must include {n}, "
                             "{time} or {datetime} -- otherwise two delivered "
                             "editions can get the same filename and the "
                             "second overwrites the first on the device. {n} "
                             "only advances once an edition has actually been "
                             "downloaded, so two rebuilds nobody has synced "
                             "yet can briefly share a title -- only the "
                             "current one is ever reachable, so that is not "
                             "a real collision."),
    "keep_delivered_editions": (5, "int", "Editions",
                                "Delivered EPUBs to keep",
                                "Older delivered files are deleted from disk."),
    "delivery_min_fraction": (0.98, "float", "Editions",
                              "Bytes needed to count as delivered",
                              "Fraction of the EPUB that must actually reach the "
                              "client before its articles are marked read."),
    "delivery_confirm_delay_s": (60, "int", "Editions", "Confirm delay (s)",
                                 "Grace period after a complete download before "
                                 "articles are marked read, so a failed sync can "
                                 "still be undone."),

    "poll_enabled": (1, "int", "Scheduling", "Polling enabled", ""),
    "assemble_interval_min": (10, "int", "Scheduling", "Assemble interval (min)", ""),
    "process_interval_min": (5, "int", "Scheduling", "Process interval (min)", ""),
    "build_interval_min": (30, "int", "Scheduling", "Build interval (min)", ""),
    "http_user_agent": ("FeedReader/1.0", "str", "Scheduling",
                        "HTTP User-Agent",
                        "Sent when fetching feeds and articles."),
    "fetch_timeout_s": (30, "int", "Scheduling", "Fetch timeout (s)", ""),

    "simplify_symbols": (1, "int", "Cleaning", "Replace decorative symbols",
                        "Small e-ink fonts have no glyph for dingbats, arrows "
                        "or emoji and draw an empty box instead. This maps the "
                        "common ones to ASCII (➧ becomes >) and drops the "
                        "rest. Ordinary punctuation -- dashes, curly quotes, "
                        "ellipses, bullets -- and all accented and non-Latin "
                        "text are left untouched."),

    "images_enabled": (1, "int", "Images", "Include images at all",
                       "Master switch, overriding the per-feed setting. Turn "
                       "it off for a reader that cannot display images: "
                       "nothing is downloaded, nothing is embedded, and the "
                       "books get markedly smaller and quicker to transfer. "
                       "Use tools/colourtest.py and tools/packagingtest.py to find out "
                       "what your reader actually supports."),
    "image_max_width": (1200, "int", "Images", "Max image width (px)",
                        "Set these two to your reader's screen size. Images "
                        "are scaled down to fit inside the box, keeping their "
                        "aspect ratio, and are never enlarged. Anything bigger "
                        "than the screen is wasted bytes -- and decoding it "
                        "costs the device roughly width x height x 2 bytes of "
                        "RAM, which a small reader may not have."),
    "image_max_height": (1600, "int", "Images", "Max image height (px)", ""),
    "image_quality": (80, "int", "Images", "JPEG quality", ""),
    "image_grayscale": (0, "int", "Images", "Convert to greyscale",
                        "Off by default. A greyscale JPEG has a single colour "
                        "component, and many small readers only decode "
                        "3-component colour JPEGs and show nothing at all. "
                        "Only turn this on if you have checked your reader "
                        "copes with it."),

    "display_timezone": ("UTC", "str", "Display", "Timezone",
                         "IANA name, e.g. Europe/Lisbon or Asia/Dubai. "
                         "Times are always stored in UTC; this only decides "
                         "how they are shown in the web UI and what date an "
                         "edition title carries."),

    "catalog_title": ("Library", "str", "Catalog", "OPDS catalog title",
                      "Shown as the name of the catalogue on the ereader."),
    "publisher_name": ("", "str", "Catalog", "Author / publisher name",
                       "Written into each EPUB and each catalogue entry. "
                       "Readers build the saved filename from this plus the "
                       "title. Leave blank to use the category name, which is "
                       "usually what you want -- your library then groups by "
                       "category."),
    "opds_log_keep": (1000, "int", "Catalog", "Access log entries to keep",
                      "Requests to /opds are logged so you can see whether the "
                      "ereader is connecting. Older rows are discarded."),
}


# Values that were once a default and have since been replaced because the old
# one was wrong. A stored value matching one of these was never chosen by the
# user -- it is just the old default sitting in their database -- so it is safe
# to move it forward. A value the user actually customised is left alone.
SUPERSEDED_DEFAULTS: dict[str, tuple[str, ...]] = {
    # "{category} - {date}" repeats for every edition built on the same day,
    # and ereaders name the saved file from the title, so each new edition
    # overwrote the last one on the device.
    # "{category} No. {n} - {date}" is fine on its own, but the entry author is
    # now the category, so it would read "Technology - Technology No. 7".
    "edition_title_format": ("{category} - {date}",
                             "{category} No. {n} - {date}"),
    # This application's name has no business in the user's library.
    "catalog_title": ("RSSOPDS",),
    "http_user_agent": ("RSSOPDS/1.0 (+self-hosted feed reader)",),
    # Greyscale JPEGs are single-component and a good many small readers
    # cannot decode them at all.
    "image_grayscale": ("1",),
}


def seed_defaults(s: Session) -> None:
    existing = {k for (k,) in s.execute(select(Setting.key))}
    for key, (default, *_rest) in DEFAULTS.items():
        if key not in existing:
            s.add(Setting(key=key, value=str(default)))

    for key, stale in SUPERSEDED_DEFAULTS.items():
        row = s.get(Setting, key)
        if row is not None and row.value in stale:
            row.value = str(DEFAULTS[key][0])


def _coerce(raw: str, kind: str) -> Any:
    try:
        if kind == "int":
            return int(float(raw))
        if kind == "float":
            return float(raw)
    except (TypeError, ValueError):
        return DEFAULTS_VALUE_FALLBACK(kind)
    return raw


def DEFAULTS_VALUE_FALLBACK(kind: str) -> Any:
    return {"int": 0, "float": 0.0}.get(kind, "")


def all_settings(s: Session) -> dict[str, Any]:
    rows = {r.key: r.value for r in s.execute(select(Setting)).scalars()}
    out: dict[str, Any] = {}
    for key, (default, kind, *_rest) in DEFAULTS.items():
        raw = rows.get(key)
        out[key] = _coerce(raw, kind) if raw is not None else default
    return out


def get(s: Session, key: str) -> Any:
    default, kind, *_rest = DEFAULTS[key]
    row = s.get(Setting, key)
    if row is None or row.value is None:
        return default
    return _coerce(row.value, kind)


def put(s: Session, key: str, value: Any) -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)


def grouped() -> dict[str, list[dict[str, Any]]]:
    """Settings metadata for rendering the settings form."""
    out: dict[str, list[dict[str, Any]]] = {}
    for key, (default, kind, group, label, help_text, *rest) in DEFAULTS.items():
        out.setdefault(group, []).append({
            "key": key, "default": default, "kind": kind,
            "label": label, "help": help_text,
            "choices": rest[0] if rest else None,
        })
    return out
