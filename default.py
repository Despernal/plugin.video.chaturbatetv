"""plugin.video.chaturbatetv entry point.

Phase 1 placeholder. Phase 2 wires this into a real router that
dispatches modes parsed out of ``sys.argv``. For now we just import
the lib package so a stray addon-load test on  confirms the
package imports cleanly.
"""
from __future__ import annotations

import sys


def main() -> None:
    """Entry-point stub. Will dispatch modes in Phase 2."""
    # Keep this lightweight; do not import xbmc* here yet.
    _ = sys.argv


if __name__ == "__main__":
    main()
