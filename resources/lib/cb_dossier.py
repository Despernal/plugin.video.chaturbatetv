"""Parse the ``initialRoomDossier`` blob out of a Chaturbate room HTML page.

The dossier is a JSON-encoded object embedded in a JS string assignment::

    window.initialRoomDossier = "{\\"username\\":\\"...\\",\\"hls_source\\":\\"...\\",...}";

We extract the quoted string with a small regex (no BeautifulSoup
needed - the dossier is in <script>, not in any DOM tree we'd want to
traverse), unescape the inner JSON, then ``json.loads`` it.

Returns a dict with these keys::

    {
        "is_live":    bool,         # hls_source present and non-empty
        "hls_source": str | None,
        "gender":     Gender,
        "name":       str,          # dossier 'username'
    }

On any parse failure (missing dossier, broken escape, json error)
returns a safe default with all fields zeroed. The TV loop must never
crash on a single bad page.
"""
from __future__ import annotations

import json
import re
from typing import Any

from resources.lib.cb_models import Gender


_DOSSIER_RE = re.compile(
    r'initialRoomDossier\s*=\s*"((?:\\.|[^"\\])*)"',
    re.DOTALL,
)


def _safe_default(name: str = "") -> dict[str, Any]:
    return {
        "is_live": False,
        "hls_source": None,
        "gender": Gender.UNKNOWN,
        "name": name,
    }


def _decode_dossier_payload(escaped: str) -> dict[str, Any] | None:
    """Reverse the JS-string-literal escaping and parse the JSON inside."""
    try:
        # The page emits a JS string literal: backslash-escaped quotes,
        # backslash-escaped slashes, unicode escapes. Round-trip through
        # latin-1 + unicode-escape gives us the original JSON text.
        raw = escaped.encode("latin-1", errors="replace").decode("unicode-escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def parse_room_dossier(html: str) -> dict[str, Any]:
    """Extract the room facts we care about from a Chaturbate room page.

    Always returns a dict with keys ``is_live`` (bool),
    ``hls_source`` (str | None), ``gender`` (Gender), ``name`` (str).
    Never raises.
    """
    if not html:
        return _safe_default()
    match = _DOSSIER_RE.search(html)
    if not match:
        return _safe_default()
    data = _decode_dossier_payload(match.group(1))
    if data is None:
        return _safe_default()
    name = str(data.get("username") or "")
    hls_raw = data.get("hls_source")
    hls = hls_raw if isinstance(hls_raw, str) and hls_raw else None
    return {
        "is_live": hls is not None,
        "hls_source": hls,
        "gender": Gender.from_str(data.get("broadcaster_gender")),
        "name": name,
    }
