"""Manifest-validation tests for addon.xml.

v0.7.39 incident: a stray ``<name>`` in the v0.7.39 news entry made
addon.xml fail to XML-parse. Kodi silently fell back to the highest-
versioned ``.bak19`` directory (v0.7.11) and the user saw "addon at
v0.7.11" instead of the freshly-deployed v0.7.39. The deploy looked
clean -- pytest green, ruff/mypy green, file copied, Kodi restarted --
because there was no test guarding the manifest itself.

These tests run every commit via the pre-commit pytest gate so a
malformed addon.xml ships RED instead of green.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


_ADDON_XML = Path(__file__).resolve().parent.parent / "addon.xml"


def test_addon_xml_parses_as_valid_xml() -> None:
    """The single most important invariant: addon.xml must parse.
    Pre-fix, a stray ``<name>`` in the news entry produced an
    XML_ERROR_MISMATCHED_ELEMENT at parse time. Kodi logged the
    error and silently fell back to a backup directory's older
    addon.xml, deploying an OLD version while every gate said green.
    """
    tree = ET.parse(_ADDON_XML)
    root = tree.getroot()
    assert root.tag == "addon"
    assert root.get("id") == "plugin.video.chaturbatetv"


def test_addon_xml_has_version_attribute() -> None:
    """Production deploys depend on the version attribute being
    present and parseable as dotted ints. Missing or malformed
    version means Kodi can't compare across multiple addon dirs and
    its "pick the highest version" rule falls back to whichever XML
    parsed last."""
    tree = ET.parse(_ADDON_XML)
    version = tree.getroot().get("version")
    assert version, "addon.xml must declare a version"
    parts = version.split(".")
    assert len(parts) == 3, (
        f"version should be major.minor.patch, got {version!r}"
    )
    for part in parts:
        assert part.isdigit(), (
            f"version part {part!r} is not a digit; got {version!r}"
        )


def test_addon_news_text_has_no_raw_angle_brackets() -> None:
    """The XML parser already enforces this (the v0.7.39 incident
    triggered XML_ERROR_MISMATCHED_ELEMENT on a stray ``<name>``),
    but a defense-in-depth sweep over the news text catches stray
    ``<`` / ``>`` even if a future change wraps the news in CDATA
    where the XML parser would otherwise tolerate them.

    The news block is free-form release notes. Authors should escape
    angle brackets as ``&lt;`` / ``&gt;`` or rephrase. Any literal
    ``<word>`` pattern in the news text means somebody pasted a
    BBCode tag, an XML element name, or HTML markup -- all of which
    have bitten us in the past.
    """
    raw = _ADDON_XML.read_text(encoding="utf-8")
    # Extract the news section from the raw text (we want the literal
    # bytes, before the XML parser normalizes entities). Match
    # everything between the first <news> and its </news>.
    m = re.search(r"<news>(.*?)</news>", raw, flags=re.DOTALL)
    assert m, "addon.xml has no <news> section"
    news_text = m.group(1)
    # Any `<word>` pattern that isn't an entity-escaped &lt;...&gt;
    # is suspicious. Entities are fine because the XML parser already
    # handled them before we got here.
    suspicious = re.findall(r"<[A-Za-z][A-Za-z0-9_-]*>", news_text)
    assert not suspicious, (
        f"raw angle-bracket markup in news text would break XML parsing: "
        f"{suspicious!r}. Escape with &lt;name&gt; or rephrase "
        f"(e.g. ``[B]NAME[/B]`` instead of ``[B]<name>[/B]``)"
    )


def test_addon_xml_extension_points_present() -> None:
    """v0.7.39 incident's downstream effect was that the .bak19
    directory's *older* addon.xml took over because ours couldn't
    parse. Kodi looks at the ``<extension point="xbmc.python.
    pluginsource">`` tag to decide what kind of addon this is; if
    the parsed manifest doesn't have it, Kodi treats this as a
    library/data addon, not a plugin, and the URL routes silently
    don't dispatch."""
    tree = ET.parse(_ADDON_XML)
    points = {
        ext.get("point") for ext in tree.findall("extension")
    }
    assert "xbmc.python.pluginsource" in points, (
        f"missing xbmc.python.pluginsource extension; got {points!r}"
    )
    assert "xbmc.addon.metadata" in points, (
        f"missing xbmc.addon.metadata extension; got {points!r}"
    )
