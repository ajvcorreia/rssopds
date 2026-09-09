"""Benchmark Ollama models on the job this app actually does.

Speed alone is the wrong measure. The cleaning prompt asks the model to
reproduce the article *verbatim* minus the furniture, so a model that is fast
because it summarises is useless -- its output gets thrown away by the
retention guard. This measures both, on real articles from the database, and
through the real ai_clean() path so chunking and the guard are included.

    python tools/benchmark_models.py                  # sensible defaults
    python tools/benchmark_models.py --models a,b,c --timeout 180
"""
from __future__ import annotations

import argparse
import functools
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Article  # noqa: E402
from app.pipeline import clean  # noqa: E402
from app.settings_store import get as setting  # noqa: E402

print = functools.partial(print, flush=True)  # long runs must report live

FENCE = re.compile(r"```")
THINK = re.compile(r"<think>|</think>", re.I)
PREAMBLE = re.compile(r"^\s*(here|sure|certainly|okay|of course|below)\b", re.I)


def sample_articles(limit: int) -> list[tuple[int, str, str]]:
    """Real article bodies, shortest first, skipping trivial ones."""
    with session_scope() as session:
        rows = session.execute(
            select(Article.id, Article.title, Article.body_html)
            .where(Article.body_html.isnot(None), Article.word_count > 150)
            .order_by(Article.word_count)
        ).all()
    picked, seen = [], set()
    for aid, title, body in rows:
        words = clean.word_count(body)
        bucket = words // 400
        if bucket in seen:
            continue
        seen.add(bucket)
        picked.append((aid, title or "(untitled)", body))
        if len(picked) >= limit:
            break
    return picked


def run(models: list[str], articles, base_url: str, timeout: int,
        num_ctx: int, keep_alive: str, min_retain: float):
    print(f"\nOllama: {base_url}   timeout {timeout}s   keep_alive {keep_alive}")
    print(f"Retention guard: output must keep >= {min_retain:.0%} of the words\n")

    for aid, title, body in articles:
        base = clean.sanitize(body)
        original = clean.word_count(base)
        print("=" * 78)
        print(f"Article {aid}: {title[:56]}")
        print(f"  {original} words after rule-based cleaning")
        print(f"  {'model':22} {'time':>8} {'kept':>7} {'w/s':>6}  verdict")
        print(f"  {'-' * 22} {'-' * 8} {'-' * 7} {'-' * 6}  {'-' * 26}")

        for model in models:
            started = time.monotonic()
            try:
                out, used = clean.ai_clean(
                    base, base_url=base_url, model=model, timeout=timeout,
                    num_ctx=num_ctx, min_retain=min_retain,
                    keep_alive=keep_alive)
                took = time.monotonic() - started
            except Exception as exc:                      # noqa: BLE001
                print(f"  {model:22} {'--':>8} {'--':>7} {'--':>6}  ERROR {exc}")
                continue

            kept = clean.word_count(out) / max(original, 1)
            rate = original / took if took else 0
            accepted = used != "rules"
            notes = []
            if not accepted:
                notes.append("REJECTED -> fell back to rules")
            if FENCE.search(out):
                notes.append("code fence")
            if THINK.search(out):
                notes.append("<think> leaked")
            if PREAMBLE.match(clean.text_of(out)):
                notes.append("chatty preamble")
            verdict = "; ".join(notes) if notes else "accepted"

            print(f"  {model:22} {took:7.1f}s {kept:6.0%} {rate:6.0f}  {verdict}")
    print("=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="llama3.2:latest,llama3.1:latest,"
                                            "qwen3.5:9b,gemma4:e4b")
    parser.add_argument("--articles", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=200)
    args = parser.parse_args()

    with session_scope() as s:
        base_url = setting(s, "ollama_url")
        num_ctx = setting(s, "ollama_num_ctx")
        keep_alive = str(setting(s, "ollama_keep_alive"))
        min_retain = setting(s, "ai_min_retain_ratio")

    picked = sample_articles(args.articles)
    if not picked:
        sys.exit("no suitable articles in the database")
    run([m.strip() for m in args.models.split(",") if m.strip()],
        picked, base_url, args.timeout, num_ctx, keep_alive, min_retain)
