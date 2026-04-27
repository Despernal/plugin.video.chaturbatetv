"""Lightweight mocks of Kodi runtime objects for offline TV-loop tests.

The TV loop tests use these so we don't have to  round-trip every
iteration. Self-tests in ``test_kodi_mock_harness.py`` keep the mocks
honest.

Quick map:

- ``MockMonitor``        - abortRequested / waitForAbort.
- ``MockPlayer``         - isPlaying / getPlayingFile, fires
                           onAVStarted / onPlayBackStopped on cue.
- ``MockPlayList``       - add / clear / getposition / size, with
                           item paths queryable via ``[i].getPath()``.
- ``MockWindow``         - get/set/clearProperty.
- ``MockGlobalIdleTime`` - controllable from tests.

Each class is small, predictable, and isolation-tested below.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


# ----------------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------------

class MockMonitor:
    """Stand-in for ``xbmc.Monitor``.

    Tests flip ``abort = True`` to simulate Kodi shutting down. The
    ``waitForAbort`` method does not actually sleep; it returns immediately,
    True if the test has set abort, False otherwise. This makes the
    TV-loop tests fast.
    """

    def __init__(self) -> None:
        self.abort = False
        self.wait_calls: list[float] = []

    def abortRequested(self) -> bool:
        return self.abort

    def waitForAbort(self, timeout: float = 0.0) -> bool:
        self.wait_calls.append(timeout)
        return self.abort


# ----------------------------------------------------------------------------
# Player
# ----------------------------------------------------------------------------

class MockPlayer:
    """Stand-in for ``xbmc.Player``.

    The harness tracks isPlaying state and the current file. Tests drive
    state changes via ``simulate_av_started(path)`` and
    ``simulate_stopped()``; the player fires the corresponding callbacks.

    Subclassing pattern matches Kodi's: a real ``_TVPlayer(xbmc.Player)``
    overrides ``onAVStarted`` / ``onPlayBackStopped``. Tests can either
    use ``MockPlayer`` directly and inspect its events list, or subclass
    it to mirror the production player.
    """

    def __init__(self) -> None:
        self._playing = False
        self._playing_file = ""
        self.events: list[tuple[str, dict[str, Any]]] = []

    # Kodi-shaped interface ---------------------------------------------------

    def isPlaying(self) -> bool:
        return self._playing

    def isPlayingVideo(self) -> bool:
        return self._playing

    def getPlayingFile(self) -> str:
        if not self._playing:
            raise RuntimeError("Player.getPlayingFile when not playing")
        return self._playing_file

    def play(self, item: Any = None, listitem: Any = None,
             windowed: bool = False, startpos: int = -1) -> None:
        # Default behaviour: mark playing with whatever item is. Tests can
        # override by setting _playing manually.
        if isinstance(item, str):
            self._playing_file = item
            self._playing = True

    def stop(self) -> None:
        if self._playing:
            self.simulate_stopped()

    # Hooks subclasses override (matches Kodi's Player API) -------------------

    def onAVStarted(self) -> None:
        pass

    def onPlayBackStopped(self) -> None:
        pass

    def onPlayBackEnded(self) -> None:
        pass

    def onPlayBackError(self) -> None:
        pass

    # Test-driven state changes -----------------------------------------------

    def simulate_av_started(self, path: str) -> None:
        self._playing = True
        self._playing_file = path
        self.events.append(("av_started", {"path": path}))
        self.onAVStarted()

    def simulate_stopped(self) -> None:
        self._playing = False
        self.events.append(("stopped", {}))
        self.onPlayBackStopped()

    def simulate_ended(self) -> None:
        self._playing = False
        self.events.append(("ended", {}))
        self.onPlayBackEnded()

    def simulate_error(self) -> None:
        self._playing = False
        self.events.append(("error", {}))
        self.onPlayBackError()


# ----------------------------------------------------------------------------
# PlayList
# ----------------------------------------------------------------------------

@dataclass
class _PlayListItem:
    path: str
    label: str = ""

    def getPath(self) -> str:
        return self.path


class MockPlayList:
    """Stand-in for ``xbmc.PlayList``.

    Items get added with ``add(url, listitem)`` (we ignore the listitem,
    just store the URL). ``getposition`` / ``size`` reflect the queue.
    Indexing ``pl[i]`` returns an object with ``getPath()`` so the TV
    loop's ``pl[pos].getPath()`` pattern works unchanged.
    """

    def __init__(self) -> None:
        self._items: list[_PlayListItem] = []
        self._position = 0

    def add(self, url: str, listitem: Any = None, index: int = -1) -> None:
        if not isinstance(url, str):
            raise TypeError(f"PlayList.add expects str url, got {type(url).__name__}")
        if index >= 0 and index <= len(self._items):
            self._items.insert(index, _PlayListItem(path=url))
        else:
            self._items.append(_PlayListItem(path=url))

    def clear(self) -> None:
        self._items = []
        self._position = 0

    def size(self) -> int:
        return len(self._items)

    def getposition(self) -> int:
        return self._position

    def __getitem__(self, index: int) -> _PlayListItem:
        return self._items[index]

    # Test helpers ------------------------------------------------------------

    def set_position(self, pos: int) -> None:
        self._position = pos

    def paths(self) -> list[str]:
        return [it.path for it in self._items]


# ----------------------------------------------------------------------------
# Window
# ----------------------------------------------------------------------------

class MockWindow:
    """Stand-in for ``xbmcgui.Window(10000)``.

    Just a dict masquerading as a property bag. Test-scoped: each test
    that instantiates a fresh ``MockWindow`` gets a clean store.
    """

    def __init__(self) -> None:
        self._props: dict[str, str] = {}

    def getProperty(self, key: str) -> str:
        return self._props.get(key, "")

    def setProperty(self, key: str, value: str) -> None:
        self._props[key] = value

    def clearProperty(self, key: str) -> None:
        self._props.pop(key, None)

    # Test helpers ------------------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the prop dict; useful for assertions."""
        return dict(self._props)


# ----------------------------------------------------------------------------
# Global idle time
# ----------------------------------------------------------------------------

class MockGlobalIdleTime:
    """Stand-in for ``xbmc.getGlobalIdleTime``.

    Tests set ``self.idle_seconds`` directly. Returned via the callable
    ``get`` so production code that calls ``xbmc.getGlobalIdleTime()`` can
    bind ``mock.get`` to that name. Or subclass and override ``get``.
    """

    def __init__(self, idle_seconds: int = 0) -> None:
        self.idle_seconds = idle_seconds

    def get(self) -> int:
        return self.idle_seconds


# ----------------------------------------------------------------------------
# Builder helper for tests
# ----------------------------------------------------------------------------

@dataclass
class MockKodiRuntime:
    """Bundled set of mocks the TV loop tests use together."""

    monitor: MockMonitor = field(default_factory=MockMonitor)
    player: MockPlayer = field(default_factory=MockPlayer)
    playlist: MockPlayList = field(default_factory=MockPlayList)
    window: MockWindow = field(default_factory=MockWindow)
    idle: MockGlobalIdleTime = field(default_factory=MockGlobalIdleTime)
    sleep_log: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        """Used in place of ``xbmc.sleep`` so the loop never blocks tests."""
        self.sleep_log.append(seconds)


def make_runtime(player_cls: Callable[..., MockPlayer] | None = None) -> MockKodiRuntime:
    """Convenience builder: returns a fresh runtime with optionally a custom Player class."""
    rt = MockKodiRuntime()
    if player_cls is not None:
        rt.player = player_cls()
    return rt
