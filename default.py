"""plugin.video.chaturbatetv entry point.

Top-level click renders the main browse menu; mode-specific URLs
dispatch to their handler in ``resources.lib.router.DEFAULT_HANDLERS``.
Browse, search, favorites, playvid, and TV mode all flow through this
single dispatch.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make resources/ importable when Kodi launches this script.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


def main() -> None:
    from resources.lib import router
    router.dispatch(sys.argv, router.DEFAULT_HANDLERS)


if __name__ == "__main__":
    main()
