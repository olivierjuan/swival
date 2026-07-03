"""Tests for swival/model_prefs.py."""

import pytest

from swival import model_prefs as mp


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    yield tmp_path


def test_empty_when_no_file():
    prefs = mp.load_prefs()
    assert prefs.favorites == {}
    assert prefs.recents == {}
    assert prefs.warning is None


def test_toggle_favorite_roundtrip():
    assert mp.toggle_favorite("huggingface", "org/model-a") is True
    assert mp.toggle_favorite("huggingface", "org/model-b") is True

    prefs = mp.load_prefs()
    assert prefs.favorites_for("huggingface") == ["org/model-a", "org/model-b"]
    assert prefs.is_favorite("huggingface", "org/model-a")

    assert mp.toggle_favorite("huggingface", "org/model-a") is False
    prefs = mp.load_prefs()
    assert prefs.favorites_for("huggingface") == ["org/model-b"]
    assert not prefs.is_favorite("huggingface", "org/model-a")


def test_favorites_are_per_provider():
    mp.toggle_favorite("huggingface", "org/model")
    mp.toggle_favorite("lmstudio", "local-model")

    prefs = mp.load_prefs()
    assert prefs.favorites_for("huggingface") == ["org/model"]
    assert prefs.favorites_for("lmstudio") == ["local-model"]
    assert prefs.favorites_for("openrouter") == []


def test_provider_aliases_normalize():
    mp.toggle_favorite("vertexai", "gemini-3-pro")
    prefs = mp.load_prefs()
    assert prefs.is_favorite("geap", "gemini-3-pro")
    assert prefs.favorites == {"geap": ["gemini-3-pro"]}


def test_recents_mru_order_and_cap():
    for i in range(mp.RECENTS_CAP + 3):
        mp.record_recent("lmstudio", f"model-{i}")
    mp.record_recent("lmstudio", "model-5")  # re-visit moves to front

    recents = mp.load_prefs().recents_for("lmstudio")
    assert recents[0] == "model-5"
    assert len(recents) == mp.RECENTS_CAP
    assert "model-0" not in recents  # evicted


def test_file_is_valid_toml_and_human_readable():
    mp.toggle_favorite("huggingface", "org/model")
    mp.record_recent("huggingface", "org/model")

    text = mp.prefs_path().read_text()
    assert text.startswith("#")
    assert "[favorites]" in text
    assert "[recents]" in text
    assert 'huggingface = ["org/model"]' in text


def test_malformed_file_is_tolerated():
    mp.prefs_path().parent.mkdir(parents=True, exist_ok=True)
    mp.prefs_path().write_text("this is [ not toml =")

    prefs = mp.load_prefs()
    assert prefs.favorites == {}
    assert prefs.warning is not None

    # Writing over the malformed file recovers.
    assert mp.toggle_favorite("lmstudio", "m") is True
    assert mp.load_prefs().warning is None


def test_preferred_for_dedupes_favorites_and_recents():
    mp.toggle_favorite("huggingface", "org/fav")
    mp.record_recent("huggingface", "org/recent")
    mp.record_recent("huggingface", "ORG/FAV")  # same model, different case

    preferred = mp.load_prefs().preferred_for("huggingface")
    assert preferred == ["org/fav", "org/recent"]


def test_wrong_types_are_dropped():
    mp.prefs_path().parent.mkdir(parents=True, exist_ok=True)
    mp.prefs_path().write_text(
        '[favorites]\nlmstudio = "not-a-list"\ngood = ["a", 3, "b"]\n'
    )
    prefs = mp.load_prefs()
    assert prefs.favorites == {"good": ["a", "b"]}
