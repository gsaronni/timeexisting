import random

import pytest

from timeexisting.content import phrases


@pytest.fixture(autouse=True)
def _clear_decks():
    phrases._decks.clear()
    yield
    phrases._decks.clear()


def test_missing_key_returns_empty_string_never_raises():
    assert phrases.pick("no.such.key") == ""


def test_missing_locale_falls_back_to_voice_en():
    resolved = phrases.pick("existential.0", locale="xx")
    reference = phrases.pick("existential.0", locale="en")
    assert resolved == reference
    assert resolved != ""


def test_missing_voice_falls_back_to_grimdark_en():
    resolved = phrases.pick("existential.0", voice="does-not-exist")
    assert resolved != ""


def test_weekday_pool_covers_monday_zero_through_sunday_six():
    for index in range(7):
        assert phrases.pick(f"weekday.{index}") != ""


def test_deque_never_repeats_within_one_cycle(monkeypatch):
    pool = ["alpha", "beta", "gamma", "delta"]

    def fake_load_pack(voice, locale):
        return {"demo": pool} if (voice, locale) == ("testvoice", "en") else {}

    monkeypatch.setattr(phrases, "_load_pack", fake_load_pack)

    rng = random.Random(0)
    drawn = [phrases.pick("demo", voice="testvoice", locale="en", rng=rng) for _ in range(len(pool))]

    assert sorted(drawn) == sorted(pool)
    assert len(set(drawn)) == len(pool)
