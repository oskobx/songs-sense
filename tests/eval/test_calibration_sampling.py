"""Checks for the calibration-set language quota (largest-remainder allocation)."""

from __future__ import annotations

from src.eval.grade_calibration import language_quota


def test_quota_sums_to_total():
    for total in (3, 10, 30):
        quota = language_quota({"en": 81, "pl": 19, "de": 9, "es": 5}, total)
        assert sum(quota.values()) == total


def test_quota_is_proportional_for_en_pl():
    # 81:19 over 10 slots -> exact 8.1 / 1.9 -> largest remainder gives 8 / 2
    assert language_quota({"en": 81, "pl": 19}, 10) == {"en": 8, "pl": 2}


def test_single_language_takes_everything():
    assert language_quota({"pl": 19}, 10) == {"pl": 10}


def test_every_present_language_gets_at_least_one():
    # es would round to 0 at this ratio; the floor rescues it from the largest pool
    quota = language_quota({"en": 500, "es": 3}, 10)
    assert quota["es"] >= 1
    assert sum(quota.values()) == 10


def test_more_languages_than_slots():
    quota = language_quota({"en": 10, "pl": 10, "de": 10, "es": 10}, 2)
    assert sum(quota.values()) == 2


def test_empty_pools_are_ignored():
    assert language_quota({"en": 10, "de": 0}, 5) == {"en": 5}
    assert language_quota({}, 5) == {}
