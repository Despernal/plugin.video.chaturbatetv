"""plugin.video.chaturbatetv entry point.

v0.1.0 is the Phase 1 install bootstrap: pure modules + tests are all
green, but no UI is wired up yet. Phase 2 will build the router and
browse views.

For v0.1.0 the entry point pops a notification so the user can see the
addon is installed and on the right version, then exits. This keeps the
install/update path testable end-to-end without any browse code.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _addon_version() -> str:
    """Read addon.xml to surface the version to the notification."""
    here = Path(__file__).resolve().parent
    try:
        for line in (here / "addon.xml").read_text().splitlines():
            if "version=" in line and "addon" in line:
                start = line.index('version="') + len('version="')
                end = line.index('"', start)
                return line[start:end]
    except OSError:
        pass
    return "0.0.0"


def main() -> None:
    """Show a welcome notification, then exit cleanly."""
    _ = sys.argv

    try:
        import xbmcgui  # type: ignore
    except ImportError:
        return

    msg = (
        "Phase 1 is installed (pure modules ready). Browse and TV mode "
        "land in Phase 2."
    )
    xbmcgui.Dialog().notification(
        f"Chaturbate TV {_addon_version()}",
        msg,
        xbmcgui.NOTIFICATION_INFO,
        7000,
    )


if __name__ == "__main__":
    main()
