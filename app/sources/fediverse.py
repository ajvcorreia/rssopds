"""Mastodon-API source -- this is how Threads (Meta) content gets in.

Threads has no RSS feed, and Meta's own Threads API only reads *your own*
account. The workable route for following other people is ActivityPub: a
Threads user who has switched on "Fediverse sharing" is followable from any
Mastodon instance as @handle@threads.net.

So the setup is:

  1. A bot account on any Mastodon instance.
  2. That account follows the Threads handles you want (the web UI can do this
     for you -- see follow_account below).
  3. Those follows go in a Mastodon list.
  4. This source polls GET /api/v1/timelines/list/:id.

Known limits, none of which are bugs in this app:
  * Only Threads accounts that opted into federation are visible at all.
  * There is no backfill. You receive posts made after the bot follows them,
    so the first poll of a new account is normally empty.
  * Replies from non-federated users never federate, so conversation context
    is partial.

The payoff is that in_reply_to_id / in_reply_to_account_id make self-thread
detection exact -- no "1/n" or timing heuristics needed anywhere.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from dateutil import parser as dateparser

from ..models import Feed
from ..settings_store import get as setting
from .base import FetchResult, RawItem, SourceError

log = logging.getLogger(__name__)

PAGE_LIMIT = 40


def _client(instance: str, token: str, timeout: int) -> httpx.Client:
    return httpx.Client(
        base_url=instance.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        follow_redirects=True,
    )


def _id_sort_key(value: str):
    """Mastodon ids are numeric strings on most instances, opaque on some."""
    return (0, int(value)) if value.isdigit() else (1, value)


def _newest(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if _id_sort_key(a) >= _id_sort_key(b) else b


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return dateparser.isoparse(value)
    except (ValueError, TypeError):
        return None


def _first_image(status: dict) -> str | None:
    for att in status.get("media_attachments") or []:
        if att.get("type") in ("image", "gifv"):
            return att.get("url") or att.get("preview_url")
    card = status.get("card") or {}
    if card.get("image"):
        return card["image"]
    return None


def _all_images(status: dict) -> list[str]:
    return [a["url"] for a in (status.get("media_attachments") or [])
            if a.get("type") in ("image", "gifv") and a.get("url")]


def _render_status_html(status: dict) -> str:
    """Status content plus its own images.

    Threads posts carry their pictures inline rather than as an article hero,
    so every attachment is kept in the body; the separate hero image is chosen
    later from the first one.
    """
    parts = [status.get("content") or ""]
    for att in status.get("media_attachments") or []:
        if att.get("type") not in ("image", "gifv") or not att.get("url"):
            continue
        alt = (att.get("description") or "").replace('"', "&quot;")
        parts.append(f'<figure><img src="{att["url"]}" alt="{alt}"/>'
                     + (f"<figcaption>{alt}</figcaption>" if alt else "")
                     + "</figure>")
    return "\n".join(p for p in parts if p)


class FediverseSource:
    def fetch(self, feed: Feed, session) -> FetchResult:
        if not feed.url or not feed.fedi_token:
            raise SourceError("instance URL and access token are both required")

        timeout = setting(session, "fetch_timeout_s")
        wanted = {a.strip().lstrip("@").lower()
                  for a in (feed.fedi_accounts or "").split(",") if a.strip()}

        path = (f"/api/v1/timelines/list/{feed.fedi_list_id}"
                if feed.fedi_list_id else "/api/v1/timelines/home")

        statuses: list[dict] = []
        # min_id (not since_id) walks *forward* from the cursor, oldest first,
        # so a burst of more than one page cannot leave a hole in the middle.
        min_id = feed.cursor
        try:
            with _client(feed.url, feed.fedi_token, timeout) as client:
                while len(statuses) < feed.max_items_per_poll:
                    params: dict[str, object] = {"limit": PAGE_LIMIT}
                    if min_id:
                        params["min_id"] = min_id
                    resp = client.get(path, params=params)
                    if resp.status_code == 401:
                        raise SourceError("access token rejected (401)")
                    if resp.status_code >= 400:
                        raise SourceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    page = resp.json()
                    if not page:
                        break
                    statuses.extend(page)
                    # Page is newest-first; advance the cursor to its newest id.
                    min_id = max((s["id"] for s in page), key=_id_sort_key)
                    if len(page) < PAGE_LIMIT:
                        break
        except httpx.HTTPError as exc:
            raise SourceError(f"fetch failed: {exc}") from exc

        cursor = feed.cursor
        items: list[RawItem] = []
        for status in statuses:
            cursor = _newest(cursor, status.get("id"))

            if status.get("reblog"):
                if feed.fedi_skip_reblogs:
                    continue
                status = status["reblog"]

            account = status.get("account") or {}
            acct = (account.get("acct") or "").lower()
            if wanted and acct not in wanted:
                continue

            content = _render_status_html(status)
            if not content.strip():
                continue

            items.append(RawItem(
                guid=str(status.get("uri") or status.get("url") or status["id"])[:512],
                url=status.get("url") or status.get("uri"),
                title=None,  # microblog posts have none; derived during assembly
                author=account.get("display_name") or account.get("acct"),
                author_key=str(account.get("id") or acct),
                summary_html=None,
                content_html=content,
                image_url=_first_image(status),
                published_at=_parse_dt(status.get("created_at")),
                reply_to_guid=(str(status["in_reply_to_id"])
                               if status.get("in_reply_to_id") else None),
                reply_to_author_key=(str(status["in_reply_to_account_id"])
                                     if status.get("in_reply_to_account_id") else None),
            ))

        # in_reply_to_id is a local status id, but guid is the federated URI.
        # Map one to the other so the assembler can chain parts together.
        local_uri = {str(s["id"]): str(s.get("uri") or s.get("url") or s["id"])
                     for s in statuses}
        for item in items:
            if item.reply_to_guid:
                item.reply_to_guid = local_uri.get(item.reply_to_guid,
                                                   item.reply_to_guid)

        return FetchResult(items=items, cursor=cursor)


# --- helpers used by the web UI ------------------------------------------

def verify(instance: str, token: str, timeout: int = 20) -> dict:
    """Check a token and return the bot account it belongs to."""
    try:
        with _client(instance, token, timeout) as client:
            resp = client.get("/api/v1/accounts/verify_credentials")
            if resp.status_code == 401:
                raise SourceError("access token rejected (401)")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"could not reach {instance}: {exc}") from exc


def lists(instance: str, token: str, timeout: int = 20) -> list[dict]:
    try:
        with _client(instance, token, timeout) as client:
            resp = client.get("/api/v1/lists")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"could not list: {exc}") from exc


def create_list(instance: str, token: str, title: str, timeout: int = 20) -> dict:
    try:
        with _client(instance, token, timeout) as client:
            resp = client.post("/api/v1/lists", data={"title": title})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"could not create list: {exc}") from exc


def follow_account(instance: str, token: str, handle: str,
                   list_id: str | None = None, timeout: int = 30) -> dict:
    """Resolve a remote handle, follow it, and optionally add it to a list.

    `handle` is what the user types in the web UI, e.g. "someone@threads.net".
    Resolution is the step that fails when a Threads account has not enabled
    fediverse sharing -- the error text says so explicitly, because that is by
    far the most common reason adding a Threads account does not work.
    """
    handle = handle.strip().lstrip("@")
    try:
        with _client(instance, token, timeout) as client:
            found = client.get("/api/v1/accounts/search",
                               params={"q": handle, "resolve": "true", "limit": 5})
            found.raise_for_status()
            matches = [a for a in found.json()
                       if (a.get("acct") or "").lower() == handle.lower()]
            if not matches:
                hint = ""
                if handle.lower().endswith("@threads.net"):
                    hint = (" Threads accounts are only reachable if the user has "
                            "turned on Fediverse sharing in their Threads settings.")
                raise SourceError(f"could not resolve @{handle}.{hint}")
            account = matches[0]

            rel = client.post(f"/api/v1/accounts/{account['id']}/follow")
            rel.raise_for_status()

            if list_id:
                added = client.post(f"/api/v1/lists/{list_id}/accounts",
                                    data={"account_ids[]": account["id"]})
                # 422 here means "already in the list", which is fine.
                if added.status_code >= 400 and added.status_code != 422:
                    raise SourceError(f"could not add to list: {added.text[:200]}")
            return account
    except httpx.HTTPError as exc:
        raise SourceError(f"follow failed: {exc}") from exc
