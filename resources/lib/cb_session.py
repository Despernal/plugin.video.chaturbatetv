"""Cookie-jar wrapper for Chaturbate sessions.

Wraps ``http.cookiejar.LWPCookieJar`` so the on-disk format matches
's ``cookies.lwp``. That gets us a free migration path for
the user's existing logged-in session.

Pure-ish: load_from / save_to touch disk, but everything else is
in-memory dict manipulation. The clock is injectable (``now`` callable)
so frozen-time tests don't need ``freezegun``.
"""
from __future__ import annotations

import http.cookiejar
import time as _time_mod
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse


_NowFn = Callable[[], float]


class Session:
    """A bag of cookies + helpers tailored for Chaturbate.

    Typical usage::

        s = Session()
        s.load_from(Path("cookies.lwp"))   # tolerant of missing file
        cookies = s.cookies_for_url("https://chaturbate.com/")
    """

    def __init__(self, now: _NowFn | None = None) -> None:
        self._jar = http.cookiejar.LWPCookieJar()
        self._now: _NowFn = now or _time_mod.time

    # I/O ---------------------------------------------------------------------

    def load_from(self, path: Path) -> None:
        """Load cookies from an LWP file. Missing file is fine; broken
        files get logged-and-continue (we don't crash on corrupt cookies).
        """
        try:
            self._jar.load(str(path), ignore_discard=False, ignore_expires=False)
        except (FileNotFoundError, http.cookiejar.LoadError, OSError):
            return

    def save_to(self, path: Path) -> None:
        """Persist the jar in LWP format. Creates parent dir if missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # LWPCookieJar.save needs a real filename; it cannot take Path on
        # older stdlib so coerce to str defensively.
        self._jar.save(str(path), ignore_discard=True, ignore_expires=True)

    # Queries -----------------------------------------------------------------

    def is_expired(self) -> bool:
        """True if there is no usable session cookie for chaturbate.com.

        We treat 'no session cookie at all' as expired since the caller
        must (re-)authenticate either way. A cookie whose ``expires``
        is in the past also counts as expired.

        The dossier endpoint anonymously serves room HTML, so missing
        cookies isn't fatal for browsing - this method is mainly for
        the auth/login layer to know when to refresh.
        """
        now = self._now()
        for cookie in self._jar:
            if "chaturbate.com" not in (cookie.domain or ""):
                continue
            if cookie.expires is not None and cookie.expires < now:
                continue
            return False
        return True

    def cookies_for_url(self, url: str) -> dict[str, str]:
        """Return the cookies that would be sent on a request to ``url``.

        We use cookiejar's own domain-matching logic via ``add_cookie_header``
        on a synthetic Request, then split the resulting Cookie header.
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return {}
        if not parsed.scheme or not parsed.netloc:
            return {}
        # urllib.request.Request is the right shape for cookiejar.
        from urllib.request import Request
        req = Request(url)  # noqa: S310 - we only use it as a cookie-jar shape
        self._jar.add_cookie_header(req)
        header = req.get_header("Cookie", "")
        if not header:
            return {}
        out: dict[str, str] = {}
        for piece in header.split(";"):
            piece = piece.strip()
            if "=" not in piece:
                continue
            k, _, v = piece.partition("=")
            out[k.strip()] = v.strip()
        return out
