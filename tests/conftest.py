"""Test configuration and shared fixtures.

Rule: every test that touches disk MUST use the `tmp_path` fixture.
Tests that write to ``~`` or to ``/storage`` are a bug.

Pure-module tests do NOT need any Kodi mocking; they just import
``resources.lib.tv_store`` etc. directly.

Tests that need Kodi mocks (later phases) take the lazy fixtures
defined below: ``mock_xbmc``, ``mock_xbmcgui``, ``mock_xbmcaddon``,
``mock_xbmcplugin``, ``mock_xbmcvfs``. They only register a mock in
``sys.modules`` when the test asks for the fixture, so pure tests stay
clean and fast.
"""
from __future__ import annotations

import sys
from typing import Any
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest


def _install_mock(name: str) -> MagicMock:
    """Install a fresh MagicMock at ``sys.modules[name]`` and return it."""
    mock = MagicMock()
    sys.modules[name] = mock
    return mock


def _uninstall_mock(name: str, prior: Any) -> None:
    """Restore prior sys.modules entry (or remove if none was set)."""
    if prior is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = prior


@pytest.fixture
def mock_xbmc() -> Iterator[MagicMock]:
    prior = sys.modules.get("xbmc")
    mock = _install_mock("xbmc")
    mock.LOGINFO = 1
    mock.LOGWARNING = 2
    mock.LOGERROR = 3
    mock.LOGDEBUG = 0
    mock.PLAYLIST_VIDEO = 1
    mock.PLAYLIST_MUSIC = 0
    yield mock
    _uninstall_mock("xbmc", prior)


@pytest.fixture
def mock_xbmcgui() -> Iterator[MagicMock]:
    prior = sys.modules.get("xbmcgui")
    mock = _install_mock("xbmcgui")
    mock.NOTIFICATION_INFO = "info"
    mock.NOTIFICATION_WARNING = "warning"
    mock.NOTIFICATION_ERROR = "error"
    yield mock
    _uninstall_mock("xbmcgui", prior)


@pytest.fixture
def mock_xbmcaddon() -> Iterator[MagicMock]:
    prior = sys.modules.get("xbmcaddon")
    mock = _install_mock("xbmcaddon")
    yield mock
    _uninstall_mock("xbmcaddon", prior)


@pytest.fixture
def mock_xbmcplugin() -> Iterator[MagicMock]:
    prior = sys.modules.get("xbmcplugin")
    mock = _install_mock("xbmcplugin")
    yield mock
    _uninstall_mock("xbmcplugin", prior)


@pytest.fixture
def mock_xbmcvfs() -> Iterator[MagicMock]:
    prior = sys.modules.get("xbmcvfs")
    mock = _install_mock("xbmcvfs")
    mock.translatePath = lambda p: p
    mock.exists = lambda p: False
    yield mock
    _uninstall_mock("xbmcvfs", prior)
