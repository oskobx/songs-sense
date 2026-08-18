"""Generate candidate vibe-search queries with an LLM, for human curation.

Writes data/eval/vibe_queries_raw.json. Nothing here is the eval set yet —
curate_queries.py turns ~140 candidates into the ~100 that survive.

Critical property: the generator never sees corpus content. Generating queries
*from songs* ("pick a song, describe it, use the description as the query")
guarantees a perfect match exists for every query and inflates every metric.
The prompt below only ever receives a language and a category.

Usage:
    python -m src.eval.generate_vibe_queries
    python -m src.eval.generate_vibe_queries --multiplier 1.6
    python -m src.eval.generate_vibe_queries --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from src.eval.groq_client import DEFAULT_MODEL, GroqError, chat
from src.eval.paths import RAW_QUERIES_PATH, ensure_eval_dir

# Final eval-set targets from the spec. Deliberately over-weights non-English
# relative to the corpus (79/8.7/5.6/4) because the language-boost mechanism is
# recent and needs coverage.
TARGET_DISTRIBUTION: dict[str, int] = {"en": 70, "pl": 15, "de": 10, "es": 5}

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "pl": "Polish",
    "de": "German",
    "es": "Spanish",
}

CATEGORIES: list[tuple[str, str]] = [
    ("emotional", 'Emotional states — e.g. "the moment you realize it\'s over"'),
    ("situational", 'Situations — e.g. "driving home alone after a long night"'),
    ("abstract", 'Abstract vibes — e.g. "nostalgic but not sad"'),
    ("sensory", 'Sensory or visual — e.g. "summer heat and cheap sunglasses"'),
    ("relational", 'Relational — e.g. "loving someone who doesn\'t know yet"'),
]

PROMPT_TEMPLATE = """Generate {n} "vibe search" queries for a lyrics search engine, in {language}.

A vibe query describes a feeling, mood, or situation someone wants songs about.
It is NOT a song title, artist name, or lyric quote.

Requirements:
- 4-12 words each
- Natural phrasing, the way a person would actually type
- Category: {category}
- Varied - do not produce near-synonyms of each other
- Written natively in {language}, not translated from English

Return one query per line, no numbering, no commentary."""

# Over-generate so curation has something to delete. 1.4x on a 100-query target
# is ~140 candidates for ~100 survivors.
DEFAULT_MULTIPLIER = 1.4

_LEADING_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_SURROUNDING_QUOTES = re.compile(r'^["\'“‘](.*)["\'”’]$')


@dataclass
class Candidate:
    raw_id: str
    query: str
    language: str
    category: str


def _clean_line(line: str) -> str:
    """Strip the numbering/bullets/quotes models add despite being told not to."""
    text = _LEADING_MARKER.sub("", line).strip()
    match = _SURROUNDING_QUOTES.match(text)
    if match:
        text = match.group(1).strip()
    return text.rstrip(".").strip()


def parse_queries(response: str) -> list[str]:
    """One query per line, minus the formatting the model was asked to omit."""
    out: list[str] = []
    for line in response.splitlines():
        text = _clean_line(line)
        if not text:
            continue
        # Drop anything that is obviously commentary rather than a query.
        if text.endswith(":") or len(text.split()) > 20:
            continue
        out.append(text)
    return out


def plan_counts(multiplier: float) -> dict[str, dict[str, int]]:
    """How many candidates to request per (language, category).

    Spreads each language's quota across the five categories as evenly as
    possible, giving the leftovers to the first categories.
    """
    plan: dict[str, dict[str, int]] = {}
    for lang, target in TARGET_DISTRIBUTION.items():
        total = max(len(CATEGORIES), round(target * multiplier))
        base, remainder = divmod(total, len(CATEGORIES))
        plan[lang] = {
            key: base + (1 if i < remainder else 0)
            for i, (key, _) in enumerate(CATEGORIES)
        }
    return plan


def generate(multiplier: float, model: str, dry_run: bool = False) -> list[Candidate]:
    plan = plan_counts(multiplier)
    category_prompts = dict(CATEGORIES)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    counter = 0

    for lang, per_category in plan.items():
        language_name = LANGUAGE_NAMES[lang]
        for category, n in per_category.items():
            if n <= 0:
                continue
            prompt = PROMPT_TEMPLATE.format(
                n=n, language=language_name, category=category_prompts[category]
            )
            if dry_run:
                print(f"--- {lang}/{category} (n={n}) ---")
                print(prompt)
                print()
                continue

            print(f"generating {n:>3} {lang}/{category}...", flush=True)
            try:
                # temperature 1.0: variety is the point here, unlike the judge.
                response = chat(
                    prompt, model=model, temperature=1.0, max_completion_tokens=2048
                )
            except GroqError as exc:
                print(f"  failed ({exc}) — skipping this batch", file=sys.stderr)
                continue

            kept = 0
            for query in parse_queries(response):
                key = query.casefold()
                if key in seen:  # exact duplicate; curation handles near-duplicates
                    continue
                seen.add(key)
                counter += 1
                candidates.append(
                    Candidate(
                        raw_id=f"raw_{counter:04d}",
                        query=query,
                        language=lang,
                        category=category,
                    )
                )
                kept += 1
            print(f"  kept {kept}", flush=True)

    return candidates


def write_raw(candidates: list[Candidate], model: str, multiplier: float) -> None:
    ensure_eval_dir()
    by_language: dict[str, int] = {}
    for candidate in candidates:
        by_language[candidate.language] = by_language.get(candidate.language, 0) + 1

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "multiplier": multiplier,
        "target_distribution": TARGET_DISTRIBUTION,
        "counts_by_language": by_language,
        "candidates": [
            {
                "raw_id": c.raw_id,
                "query": c.query,
                "language": c.language,
                "category": c.category,
            }
            for c in candidates
        ],
    }
    RAW_QUERIES_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate candidate vibe queries")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--multiplier",
        type=float,
        default=DEFAULT_MULTIPLIER,
        help="Over-generation factor over the 100-query target (default: 1.4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompts that would be sent, make no API calls",
    )
    args = parser.parse_args()

    candidates = generate(args.multiplier, args.model, args.dry_run)
    if args.dry_run:
        return

    if not candidates:
        print("No candidates generated.", file=sys.stderr)
        sys.exit(1)

    write_raw(candidates, args.model, args.multiplier)

    print(f"\nWrote {len(candidates)} candidates to {RAW_QUERIES_PATH}")
    for lang, target in TARGET_DISTRIBUTION.items():
        got = sum(1 for c in candidates if c.language == lang)
        print(f"  {lang}: {got:>3} candidates (target after curation: {target})")
    print("\nNext: python -m src.eval.curate_queries")


if __name__ == "__main__":
    main()
