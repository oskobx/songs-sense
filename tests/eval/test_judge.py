"""Offline checks for judge response parsing and cache behaviour (no API calls)."""

from __future__ import annotations

import json

from src.eval import judge
from src.eval.judge import JudgeCache, JudgePair


def _pair(query_id: str = "vibe_001", passage_id: int = 42) -> JudgePair:
    return JudgePair(
        query_id=query_id,
        query="feeling lost in a city at night",
        passage_id=passage_id,
        artist="The Weeknd",
        title="Blinding Lights",
        passage_text="The city's cold and empty\nNo one's around to judge me",
        language="en",
    )


def test_parse_grade_bare_digits():
    for text, expected in [("0", 0), ("1", 1), ("2", 2), ("3", 3), ("  2  \n", 2)]:
        assert judge.parse_grade(text) == expected


def test_parse_grade_wrapped_answers():
    assert judge.parse_grade("Grade: 2") == 2
    assert judge.parse_grade("**3**") == 3
    assert judge.parse_grade("The passage is tangentially related, so 1") == 1


def test_parse_grade_unparseable():
    assert judge.parse_grade("") is None
    assert judge.parse_grade("I cannot grade this passage.") is None
    assert judge.parse_grade("7") is None


def test_build_prompt_contains_pair_fields():
    prompt = judge.build_prompt(_pair())
    assert "feeling lost in a city at night" in prompt
    assert "The Weeknd - Blinding Lights" in prompt
    assert "No one's around to judge me" in prompt
    assert prompt.rstrip().endswith("Respond with ONLY a single digit: 0, 1, 2, or 3.")


def test_cache_roundtrip(tmp_path):
    path = tmp_path / "judge_cache.json"
    cache = JudgeCache(path=path, signature="test::v1")
    pair = _pair()

    assert cache.get(pair) is None
    cache.set(pair, 3)
    cache.save()

    reloaded = JudgeCache(path=path, signature="test::v1")
    assert reloaded.get(pair) == 3
    assert reloaded.get(_pair(passage_id=99)) is None


def test_cache_is_namespaced_by_signature(tmp_path):
    path = tmp_path / "judge_cache.json"
    pair = _pair()

    old = JudgeCache(path=path, signature="test::v1")
    old.set(pair, 3)
    old.save()

    # A changed judge prompt must not reuse the old judge's grades...
    new = JudgeCache(path=path, signature="test::v2")
    assert new.get(pair) is None
    new.set(pair, 1)
    new.save()

    # ...but the old ones survive on disk.
    stored = json.loads(path.read_text())
    assert stored["test::v1"][pair.cache_key] == 3
    assert stored["test::v2"][pair.cache_key] == 1


def test_cache_key_is_query_and_passage():
    assert _pair("vibe_007", 123).cache_key == "vibe_007|123"


def test_judge_all_is_keyed_by_cache_key_string(tmp_path, monkeypatch):
    """Regression: dict(judge_pairs(...)) keys by JudgePair object, not cache_key.

    Callers look grades up as f"{query_id}|{passage_id}", so judge_all must
    return string keys or every lookup raises KeyError after a full judging run.
    """
    monkeypatch.setattr(judge, "judge_pair", lambda *a, **k: 2)

    cache = JudgeCache(path=tmp_path / "judge_cache.json", signature="test::v1")
    pairs = [_pair("vibe_015", 55616), _pair("vibe_004", 13991)]
    graded = judge.judge_all(pairs, cache, progress=False)

    assert set(graded) == {"vibe_015|55616", "vibe_004|13991"}
    assert all(isinstance(key, str) for key in graded)
    # the exact lookup calibrate.py and run_vibe_eval.py perform
    assert graded[f"{'vibe_015'}|{55616}"] == 2


def test_judge_pairs_uses_cache_instead_of_calling_api(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("judge_pair must not be called when the grade is cached")

    monkeypatch.setattr(judge, "judge_pair", explode)

    path = tmp_path / "judge_cache.json"
    cache = JudgeCache(path=path, signature="test::v1")
    pairs = [_pair(passage_id=1), _pair(passage_id=2)]
    for pair in pairs:
        cache.set(pair, 2)

    graded = dict(judge.judge_pairs(pairs, cache, progress=False))
    assert list(graded.values()) == [2, 2]
    assert cache.hits == 2
    assert cache.misses == 0
