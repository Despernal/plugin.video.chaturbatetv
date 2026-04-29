"""Tests for resources.lib.cb_models - dataclass primitives."""
from __future__ import annotations

import dataclasses

import pytest

from resources.lib.cb_models import Favorite, Gender, Model, TVEntry


# Gender ----------------------------------------------------------------------

def test_gender_enum_has_expected_members() -> None:
    assert Gender.FEMALE.value == "female"
    assert Gender.MALE.value == "male"
    assert Gender.COUPLE.value == "couple"
    assert Gender.TRANS.value == "trans"
    assert Gender.UNKNOWN.value == "unknown"


def test_gender_from_str_known_values() -> None:
    assert Gender.from_str("f") is Gender.FEMALE
    assert Gender.from_str("female") is Gender.FEMALE
    assert Gender.from_str("m") is Gender.MALE
    assert Gender.from_str("c") is Gender.COUPLE
    assert Gender.from_str("t") is Gender.TRANS


def test_gender_from_str_unknown_falls_back() -> None:
    assert Gender.from_str("xyz") is Gender.UNKNOWN
    assert Gender.from_str("") is Gender.UNKNOWN
    assert Gender.from_str(None) is Gender.UNKNOWN


# Model -----------------------------------------------------------------------

def test_model_construction() -> None:
    m = Model(
        name="alice",
        slug="alice",
        url="https://chaturbate.com/alice/",
        is_live=True,
        viewers=42,
        gender=Gender.FEMALE,
    )
    assert m.name == "alice"
    assert m.slug == "alice"
    assert m.is_live is True
    assert m.viewers == 42
    assert m.gender is Gender.FEMALE


def test_model_equality_by_value() -> None:
    a = Model(name="alice", slug="alice", url="u", is_live=True, viewers=10, gender=Gender.FEMALE)
    b = Model(name="alice", slug="alice", url="u", is_live=True, viewers=10, gender=Gender.FEMALE)
    c = Model(name="alice", slug="alice", url="u", is_live=False, viewers=10, gender=Gender.FEMALE)
    assert a == b
    assert a != c


def test_model_from_dossier_minimal() -> None:
    m = Model.from_dossier({
        "username": "alice",
        "room_status": "public",
        "hls_source": "https://edge.chaturbate.com/.../playlist.m3u8",
        "num_users": 123,
        "broadcaster_gender": "f",
    })
    assert m.name == "alice"
    assert m.slug == "alice"
    assert m.url == "https://chaturbate.com/alice/"
    assert m.is_live is True
    assert m.viewers == 123
    assert m.gender is Gender.FEMALE


def test_model_from_dossier_offline() -> None:
    m = Model.from_dossier({
        "username": "bob",
        "room_status": "offline",
        "hls_source": "",
        "broadcaster_gender": "m",
    })
    assert m.is_live is False
    assert m.viewers == 0
    assert m.gender is Gender.MALE


def test_model_from_dossier_handles_missing_keys() -> None:
    m = Model.from_dossier({"username": "ghost"})
    assert m.name == "ghost"
    assert m.slug == "ghost"
    assert m.url == "https://chaturbate.com/ghost/"
    assert m.is_live is False
    assert m.viewers == 0
    assert m.gender is Gender.UNKNOWN


def test_model_from_dossier_couple_gender() -> None:
    m = Model.from_dossier({"username": "duo", "broadcaster_gender": "c"})
    assert m.gender is Gender.COUPLE


def test_model_from_dossier_trans_gender() -> None:
    m = Model.from_dossier({"username": "tt", "broadcaster_gender": "t"})
    assert m.gender is Gender.TRANS


def test_model_from_dossier_hidden_status_NOT_live() -> None:
    """v0.7.32: pre-fix, ``from_dossier`` flagged ``is_live`` purely on
    a non-empty ``hls_source``. A stale or cached HLS URL on a
    now-private/away/hidden room would pass the check, exposing the
    silent-stub-loop family the v0.7.31 affiliate fix neutralized.
    Now requires ``room_status == 'public'`` AND non-empty hls."""
    m = Model.from_dossier({
        "username": "ms",
        "room_status": "hidden",
        "hls_source": "https://edge.chaturbate.com/.../playlist.m3u8",
        "num_users": 100,
        "broadcaster_gender": "f",
    })
    assert m.is_live is False
    assert m.status == "hidden"


def test_model_from_dossier_public_no_hls_NOT_live() -> None:
    """Both conditions required: public AND non-empty HLS. Public
    without HLS (transient broadcasting blip) is not playable."""
    m = Model.from_dossier({
        "username": "x",
        "room_status": "public",
        "hls_source": "",
        "broadcaster_gender": "f",
    })
    assert m.is_live is False
    assert m.status == "public"


def test_model_from_dossier_records_status_field() -> None:
    """v0.7.32: ``status`` carries the lowercase room_status so views
    can render non-public broadcasters with state-prefix labels
    instead of dropping them silently."""
    for raw_status in ("public", "hidden", "private", "away",
                       "password protected", "offline"):
        m = Model.from_dossier({
            "username": "x", "room_status": raw_status,
            "hls_source": "irrelevant",
        })
        assert m.status == raw_status, (
            f"Model.status should round-trip room_status, "
            f"got {m.status!r} for input {raw_status!r}"
        )


def test_model_serialize_round_trip_via_asdict() -> None:
    m = Model(
        name="alice", slug="alice", url="u", is_live=True, viewers=10, gender=Gender.FEMALE,
    )
    d = dataclasses.asdict(m)
    # Enum survives via its value
    assert d["gender"] == Gender.FEMALE
    # Reconstructible
    d["gender"] = Gender(d["gender"])
    rebuilt = Model(**d)
    assert rebuilt == m


# TVEntry ---------------------------------------------------------------------

def test_tventry_construction() -> None:
    e = TVEntry(name="alice", url="https://chaturbate.com/alice/", priority=10)
    assert e.name == "alice"
    assert e.url == "https://chaturbate.com/alice/"
    assert e.priority == 10


def test_tventry_equality() -> None:
    a = TVEntry(name="a", url="u", priority=5)
    b = TVEntry(name="a", url="u", priority=5)
    c = TVEntry(name="a", url="u", priority=6)
    assert a == b
    assert a != c


def test_tventry_priority_can_be_any_int() -> None:
    # Validation policy: any int permitted; clamping is the caller's job.
    TVEntry(name="a", url="u", priority=-10)
    TVEntry(name="a", url="u", priority=999)


def test_tventry_priority_must_be_int() -> None:
    with pytest.raises((TypeError, ValueError)):
        TVEntry(name="a", url="u", priority="ten")  # type: ignore[arg-type]


def test_tventry_round_trip_via_asdict() -> None:
    e = TVEntry(name="alice", url="u", priority=10)
    d = dataclasses.asdict(e)
    assert d == {"name": "alice", "url": "u", "priority": 10}
    assert TVEntry(**d) == e


# Favorite --------------------------------------------------------------------

def test_favorite_construction() -> None:
    f = Favorite(name="alice", url="u", slug="alice", gender=Gender.FEMALE)
    assert f.name == "alice"
    assert f.slug == "alice"
    assert f.gender is Gender.FEMALE


def test_favorite_equality() -> None:
    a = Favorite(name="a", url="u", slug="a", gender=Gender.FEMALE)
    b = Favorite(name="a", url="u", slug="a", gender=Gender.FEMALE)
    assert a == b


def test_favorite_round_trip_via_asdict() -> None:
    f = Favorite(name="alice", url="u", slug="alice", gender=Gender.FEMALE)
    d = dataclasses.asdict(f)
    d["gender"] = Gender(d["gender"])
    assert Favorite(**d) == f
