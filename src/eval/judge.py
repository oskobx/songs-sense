"""LLM-as-judge: grade (query, passage) pairs 0-3, with an on-disk cache.

The cache is the load-bearing part. A full run is 100 queries x 10 results =
1000 judgments; at ~28 RPM that is ~35 minutes. Re-running against unchanged
retrieval must cost near-zero calls, otherwise the eval is too slow to iterate
against and stops being used.

Cache keys are (query_id, passage_id) as the spec requires, namespaced by
model + prompt version. Changing the judge prompt (e.g. adding few-shot
examples after a weak calibration) therefore invalidates old judgments instead
of silently mixing two judges' opinions in one results file.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from src.eval.groq_client import DEFAULT_MODEL, GroqError, chat
from src.eval.paths import JUDGE_CACHE_PATH, ensure_eval_dir

# Every prompt version ever used, kept so old runs stay reproducible and so any
# version can be re-measured against the calibration set. The cache namespaces
# judgments by version, so these never mix.
#
# v1 -> v2: v1 scored quadratic-weighted kappa 0.243 against 30 human-graded
# pairs, driven by a single failure mode — it rewarded topical and lexical
# adjacency, grading 2 where the human graded 1 on 10 of 30 pairs. No monotone
# rescaling of v1's output could exceed kappa 0.265, so this is a judgment
# error, not a threshold offset. v2 makes the 1-vs-2 boundary explicit and
# names surface-feature overlap as insufficient for a 2.
#
# v2 -> v3: v2 fixed the 1-vs-2 line (its "relevant" count fell from 20/30 to
# 11/30 against the human's 10/30) but collapsed the top of the scale, awarding
# grade 3 to 0 of 30 pairs and grading all three human 3s as 1. Kappa fell to
# 0.100; excluding those three pairs it beat v1, so the whole regression was in
# the top grade. v3 keeps v2's 1-vs-2 rule and repairs the ceiling: it states
# that 3 is expected when a passage lands the feeling, and drops the phrasings
# ("apply it strictly", "at most", "0 if the feeling is plainly absent") that
# read as global caution rather than a rule about one boundary.

PROMPT_TEMPLATES: dict[str, str] = {}

PROMPT_TEMPLATES[
    "v1"
] = """You are grading whether a song passage matches a "vibe search" query.

Query: {query}
Song: {artist} - {title}
Passage:
{passage_text}

Grade the match on this scale:
3 = excellent, this passage captures exactly the vibe described
2 = good, clearly relevant to the vibe
1 = marginal, tangentially related
0 = not relevant

Judge the passage's emotional and thematic content, not word overlap.
A passage in a different language than the query can still be an excellent match.
{few_shot_block}
Respond with ONLY a single digit: 0, 1, 2, or 3."""

PROMPT_TEMPLATES[
    "v2"
] = """You are grading whether a song passage matches a "vibe search" query.

Query: {query}
Song: {artist} - {title}
Passage:
{passage_text}

A vibe query describes a feeling, mood, or emotional situation. Grade how well
this passage delivers that feeling.

3 = excellent, the passage captures exactly the feeling the query describes
2 = good, the passage is clearly about this feeling
1 = marginal, related in subject matter or imagery but not in feeling
0 = not relevant, neither the feeling nor the subject matter matches

The 1-vs-2 boundary decides most passages. Apply it strictly:

- Award a 2 only when the emotional register of the passage matches the
  emotional register of the query. Ask: is this passage ABOUT the feeling the
  query names?
- A passage that merely MENTIONS something the query mentions is a 1, not a 2.
  Shared nouns, places, seasons, times of day, weather, or activities are
  surface features. Sharing them is not sharing a vibe.
- If the subject matter overlaps but the emotional register differs - triumphant
  where the query is anxious, or pining for one person where the query is
  open-ended - grade it 1 at most, and 0 if the feeling is plainly absent.
- Word overlap between the query and the passage is not evidence of a match.
  Ignore it and judge the feeling.

A passage in a different language than the query can still be an excellent
match. Judge the feeling, not the language.
{few_shot_block}
Respond with ONLY a single digit: 0, 1, 2, or 3."""

PROMPT_TEMPLATES[
    "v3"
] = """You are grading whether a song passage matches a "vibe search" query.

Query: {query}
Song: {artist} - {title}
Passage:
{passage_text}

A vibe query describes a feeling, mood, or emotional situation. Grade how well
this passage delivers that feeling.

3 = excellent, the passage captures exactly the feeling the query describes
2 = good, the passage is clearly about this feeling
1 = marginal, related in subject matter or imagery but not in feeling
0 = not relevant, neither the feeling nor the subject matter matches

Grade 3 is a common and expected outcome. When a passage lands the feeling
squarely, award a 3. It is not reserved for rare perfection.

The 1-vs-2 boundary decides most passages. Use it as follows:

- Award a 2 only when the emotional register of the passage matches the
  emotional register of the query. Ask: is this passage ABOUT the feeling the
  query names?
- A passage that merely MENTIONS something the query mentions is a 1, not a 2.
  Shared nouns, places, seasons, times of day, weather, or activities are
  surface features. Sharing them is not sharing a vibe.
- If the subject matter overlaps but the emotional register differs - triumphant
  where the query is anxious, or pining for one person where the query is
  open-ended - grade it 1.
- Word overlap between the query and the passage is not evidence of a match.
  Ignore it and judge the feeling.

A passage in a different language than the query can still be an excellent
match. Judge the feeling, not the language.
{few_shot_block}
Respond with ONLY a single digit: 0, 1, 2, or 3."""

# The active version. Bump when adding a new entry above.
PROMPT_VERSION = "v3"

# Kept for callers that want the active template directly.
PROMPT_TEMPLATE = PROMPT_TEMPLATES[PROMPT_VERSION]

# A judgment is one digit. gpt-oss also emits reasoning tokens against this same
# budget, so this is not as tight as it looks — see the truncation warning in
# groq_client, which fires if a response ever hits the ceiling.
JUDGE_MAX_TOKENS = 150

# Budget for the final attempt when the normal one keeps coming back unparseable.
# gpt-oss spends reasoning from the same allowance, so the usual suspect is a
# response truncated before the digit was emitted.
JUDGE_RETRY_MAX_TOKENS = 400

# Deliberately empty. Drawing few-shot examples from the calibration set and
# then measuring kappa on that same set is training on the test set: the score
# would rise partly because the judge had been shown the answers. Adding
# examples requires held-out pairs first.
# Each entry would be: (query, artist, title, passage_text, grade).
FEW_SHOT_EXAMPLES: list[tuple[str, str, str, str, int]] = []


class JudgeError(RuntimeError):
    """The judge could not produce a parseable grade for a pair."""


@dataclass(frozen=True)
class JudgePair:
    """One thing to grade: a query paired with one retrieved passage."""

    query_id: str
    query: str
    passage_id: int
    artist: str
    title: str
    passage_text: str
    language: str | None = None

    @property
    def cache_key(self) -> str:
        return f"{self.query_id}|{self.passage_id}"


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def _few_shot_block() -> str:
    if not FEW_SHOT_EXAMPLES:
        return ""
    lines = ["\nExamples of correct grading:"]
    for query, artist, title, passage_text, grade in FEW_SHOT_EXAMPLES:
        lines.append(
            f"\nQuery: {query}\nSong: {artist} - {title}\nPassage:\n{passage_text}\nGrade: {grade}"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def build_prompt(pair: JudgePair, version: str | None = None) -> str:
    template = PROMPT_TEMPLATES[version or PROMPT_VERSION]
    return template.format(
        query=pair.query,
        artist=pair.artist,
        title=pair.title,
        passage_text=pair.passage_text.strip(),
        few_shot_block=_few_shot_block(),
    )


def prompt_signature(model: str = DEFAULT_MODEL, version: str | None = None) -> str:
    """Cache namespace: judgments are only reusable under the same judge."""
    suffix = f"+{len(FEW_SHOT_EXAMPLES)}shot" if FEW_SHOT_EXAMPLES else ""
    return f"{model}::{version or PROMPT_VERSION}{suffix}"


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

_DIGIT_RE = re.compile(r"\b([0-3])\b")


def parse_grade(text: str) -> int | None:
    """Extract the grade from a judge response, or None if unparseable.

    gpt-oss usually returns a bare digit, but occasionally wraps it ("Grade: 2",
    "**2**"). Falls back to the last standalone 0-3 in the text, which is right
    for trailing-answer formats and harmless for bare digits.
    """
    stripped = text.strip()
    if len(stripped) == 1 and stripped in "0123":
        return int(stripped)

    matches = _DIGIT_RE.findall(stripped)
    if not matches:
        return None
    return int(matches[-1])


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


class JudgeCache:
    """JSON-backed {signature: {query_id|passage_id: grade}}."""

    def __init__(self, path: Path = JUDGE_CACHE_PATH, signature: str | None = None):
        self.path = path
        self.signature = signature or prompt_signature()
        self._data: dict[str, dict[str, int]] = self._load()
        self._data.setdefault(self.signature, {})
        self.hits = 0
        self.misses = 0

    def _load(self) -> dict[str, dict[str, int]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(
                f"warning: {self.path} is corrupt, starting a fresh cache",
                file=sys.stderr,
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    @property
    def _bucket(self) -> dict[str, int]:
        return self._data[self.signature]

    def get(self, pair: JudgePair) -> int | None:
        return self._bucket.get(pair.cache_key)

    def set(self, pair: JudgePair, grade: int) -> None:
        self._bucket[pair.cache_key] = grade

    def save(self) -> None:
        ensure_eval_dir()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)  # atomic; a Ctrl-C mid-write can't corrupt the cache

    def __len__(self) -> int:
        return len(self._bucket)


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #


def judge_pair(
    pair: JudgePair, model: str = DEFAULT_MODEL, version: str | None = None
) -> int:
    """Grade one pair with a live LLM call (no cache).

    Three attempts: two at the normal token budget, then one at
    JUDGE_RETRY_MAX_TOKENS in case the answer was being truncated by reasoning
    tokens. Nothing is ever silently graded 0.

    Both failure modes raise rather than fabricate a grade. A cached 0 is
    indistinguishable from a genuine "not relevant" judgment, so a fabricated
    one is permanent, invisible, and drags every metric down with no way to find
    it later. Aborting is recoverable: the cache flushes every 20 judgments, so
    a re-run resumes from the last flush.
    """
    prompt = build_prompt(pair, version)
    budgets = [JUDGE_MAX_TOKENS, JUDGE_MAX_TOKENS, JUDGE_RETRY_MAX_TOKENS]

    for attempt, budget in enumerate(budgets, 1):
        try:
            response = chat(prompt, model=model, max_completion_tokens=budget)
        except GroqError as exc:
            raise GroqError(f"judging {pair.cache_key} failed: {exc}") from exc
        grade = parse_grade(response)
        if grade is not None:
            return grade
        print(
            f"judge: unparseable response for {pair.cache_key} "
            f"(attempt {attempt}/{len(budgets)}, budget {budget}): "
            f"{response[:120]!r}",
            file=sys.stderr,
        )

    raise JudgeError(
        f"{pair.cache_key}: no parseable grade after {len(budgets)} attempts "
        f"(final budget {JUDGE_RETRY_MAX_TOKENS}). Refusing to cache a "
        f"fabricated 0."
    )


def judge_pairs(
    pairs: Iterable[JudgePair],
    cache: JudgeCache | None = None,
    model: str = DEFAULT_MODEL,
    save_every: int = 20,
    progress: bool = True,
    version: str | None = None,
) -> Iterator[tuple[JudgePair, int]]:
    """Yield (pair, grade), cache-first. Saves periodically so a Ctrl-C is cheap."""
    cache = (
        cache if cache is not None else JudgeCache(signature=prompt_signature(model))
    )
    pending = 0

    for index, pair in enumerate(pairs, 1):
        cached = cache.get(pair)
        if cached is not None:
            cache.hits += 1
            yield pair, cached
            continue

        grade = judge_pair(pair, model=model, version=version)
        cache.set(pair, grade)
        cache.misses += 1
        pending += 1
        if progress:
            print(
                f"  judged {index}: {pair.query_id} x passage {pair.passage_id} -> {grade}",
                flush=True,
            )
        if pending >= save_every:
            cache.save()
            pending = 0
        yield pair, grade

    if pending:
        cache.save()


def judge_all(
    pairs: list[JudgePair],
    cache: JudgeCache | None = None,
    model: str = DEFAULT_MODEL,
    progress: bool = True,
    version: str | None = None,
) -> dict[str, int]:
    """Grade every pair, returning {cache_key: grade}."""
    cache = (
        cache if cache is not None else JudgeCache(signature=prompt_signature(model))
    )
    grades = {
        p.cache_key: g
        for p, g in judge_pairs(pairs, cache, model, progress=progress, version=version)
    }
    cache.save()
    return grades
