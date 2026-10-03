"""Native playlist entries: a bare id is the file's default tune, and an
explicit ``{id, subsong: 0}`` is tune 1 — kept distinct, because a multi-tune
file's default can be another tune (its first one is empty)."""
from soniqboom.api.playlist import _entry_key, _entry_sub, _norm_entry


def test_explicit_tune_one_is_not_the_default_tune():
    assert _entry_sub("abc") is None
    assert _entry_sub({"id": "abc"}) is None
    assert _entry_sub({"id": "abc", "subsong": 0}) == 0
    assert _entry_sub({"id": "abc", "subsong": 3}) == 3
    assert _entry_key("abc") != _entry_key({"id": "abc", "subsong": 0})


def test_stored_form_keeps_the_pin():
    assert _norm_entry("abc") == "abc"
    assert _norm_entry({"id": "abc", "subsong": 0}) == {"id": "abc", "subsong": 0}
    assert _norm_entry({"id": "abc", "subsong": 2}) == {"id": "abc", "subsong": 2}


def test_junk_subsongs_fall_back_to_the_default_tune():
    for bad in (-1, True, False, "1", 1.5, None):
        assert _entry_sub({"id": "abc", "subsong": bad}) is None
        assert _norm_entry({"id": "abc", "subsong": bad}) == "abc"
