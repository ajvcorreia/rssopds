"""Optional HTTP basic auth, applied separately to the web UI and to OPDS.

Both are off when no username is configured, which is the sane default for a
service on a home LAN. They are configured separately because the ereader
needs a credential you are willing to type on a device keyboard, while the web
UI can have a long one -- and because some readers cannot do HTTP auth at all,
which is what `opds_public` is for.

Credentials come from the environment (or a .env file), never from the
database, so they are readable before anything else starts and are never
served back out through the web UI.
"""
from __future__ import annotations

import base64
import binascii
import secrets

from fastapi import HTTPException, Request, status

from .config import config


def _equal(supplied: str, expected: str) -> bool:
    """Constant-time compare that tolerates non-ASCII.

    secrets.compare_digest raises TypeError on str containing non-ASCII, so a
    password with an accent in it would turn every login into a 500. Comparing
    the UTF-8 bytes avoids that while keeping the timing guarantee.
    """
    return secrets.compare_digest(supplied.encode("utf-8"),
                                  expected.encode("utf-8"))


def _check(request: Request, accepted: list[tuple[str, str]], realm: str) -> None:
    """Allow the request if it matches any of `accepted` (user, password)."""
    pairs = [(u, p) for u, p in accepted if u]
    if not pairs:
        return  # auth disabled

    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": f'Basic realm="{realm}", charset="UTF-8"'},
    )

    header = request.headers.get("authorization", "")
    if not header[:6].lower() == "basic ":
        raise unauthorized

    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True)
        got_user, sep, got_password = decoded.decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise unauthorized from None
    if not sep:
        raise unauthorized

    # Every candidate is compared in full, so the response time reveals
    # neither which half was wrong nor which credential was tried.
    matched = False
    for user, password in pairs:
        ok_user = _equal(got_user, user)
        ok_password = _equal(got_password, password)
        matched |= ok_user and ok_password
    if not matched:
        raise unauthorized


def require_web(request: Request) -> None:
    _check(request, [(config.web_user, config.web_password)], "Library")


def require_opds(request: Request) -> None:
    # Some readers cannot send HTTP credentials at all. opds_public leaves the
    # catalogue open while the web UI stays protected.
    if config.opds_public:
        return
    # The device credential, if one is configured, plus the web credential --
    # which is strictly more privileged, so refusing it here would achieve
    # nothing except locking the administrator out of their own catalogue.
    _check(request, [(config.opds_user, config.opds_password),
                     (config.web_user, config.web_password)], "OPDS Catalog")
