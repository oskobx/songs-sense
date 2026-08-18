"""Human grading CLI for the calibration set.

Two jobs, in order:

1. Sample 30 (query, passage) pairs spanning the grade range — 10 each from
   ranks 1-3, 4-10 and 15-30. Sampling only top hits would produce a set with
   almost no 0s and 1s, and the resulting kappa would be meaningless.
2. Show them one at a time and record a 0-3 grade per keypress.

This module never imports the judge and never displays a judge grade. Anchoring
the human to the judge's opinion would make the calibration worthless.

Only queries in --languages (default en,pl) are sampled, because a grader who
cannot read a language cannot produce a trustworthy grade in it. The cost is
that the kappa then validates the judge on those languages only — de and es
metrics in the full eval inherit a judge that was never checked against a human
on those languages. Say so in the writeup rather than implying the kappa covers
everything.

Pairs are stamped with a batch number. --add appends a fresh batch drawn from
queries the set has never used, which is what makes a held-out split possible:
fit the judge prompt on one batch, report the kappa on another.

Usage:
    python -m src.eval.grade_calibration
    python -m src.eval.grade_calibration --languages en,pl,de
    python -m src.eval.grade_calibration --resample --seed 7
    python -m src.eval.grade_calibration --add 25    # append a new batch

Keys:
    3 = excellent   2 = good   1 = marginal   0 = not relevant
    u = undo previous      q = save and quit
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime

from src.eval.paths import CALIBRATION_PATH, QUERIES_PATH, ensure_eval_dir
from src.eval.retrieval_runner import (
    RetrievedPassage,
    config_description,
    db_connection,
    retrieve_vibe,
)
from src.utils.keypress import getch

TARGET_PAIRS = 30
QUERIES_TO_SAMPLE = 10  # x 3 bands = 30 pairs, 10 per band

# (name, inclusive 1-indexed rank range). The deep band is where 0s and 1s live.
BANDS: list[tuple[str, tuple[int, int]]] = [
    ("top", (1, 3)),
    ("mid", (4, 10)),
    ("deep", (15, 30)),
]
RETRIEVAL_DEPTH = 30

# Languages the grader can judge reliably. Sampling outside this set produces
# grades the kappa should not be computed from.
DEFAULT_LANGUAGES: list[str] = ["en", "pl"]

GRADE_LEGEND = "3 = excellent   2 = good   1 = marginal   0 = not relevant"


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


def load_queries(
    languages: list[str], exclude_query_ids: set[str] | None = None
) -> list[dict]:
    if not QUERIES_PATH.exists():
        print(
            f"{QUERIES_PATH} not found. Run generate_vibe_queries then curate_queries.",
            file=sys.stderr,
        )
        sys.exit(1)
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]
    already_used = exclude_query_ids or set()
    eligible = [
        q for q in queries if q["language"] in languages and q["id"] not in already_used
    ]
    if not eligible:
        print(
            f"No unused queries in the eval set match --languages "
            f"{','.join(languages)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    wrong_language = sum(1 for q in queries if q["language"] not in languages)
    print(
        f"Sampling from {len(eligible)} {'/'.join(languages)} queries "
        f"({wrong_language} excluded as ungradeable"
        + (f", {len(already_used)} already in the set" if already_used else "")
        + ")."
    )
    return eligible


def language_quota(pool_sizes: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` query slots across languages proportional to their pool sizes.

    Largest-remainder allocation so the parts sum to exactly `total`, then a
    floor of 1 for any language that has queries at all — a calibration set with
    zero Polish pairs would say nothing about the multilingual route.
    """
    langs = sorted(lang for lang, n in pool_sizes.items() if n > 0)
    if not langs:
        return {}
    if total <= len(langs):
        return {lang: 1 for lang in langs[:total]}

    grand = sum(pool_sizes[lang] for lang in langs)
    exact = {lang: pool_sizes[lang] / grand * total for lang in langs}
    quota = {lang: int(exact[lang]) for lang in langs}

    leftover = total - sum(quota.values())
    by_remainder = sorted(langs, key=lambda lang: exact[lang] % 1, reverse=True)
    for lang in by_remainder[:leftover]:
        quota[lang] += 1

    for lang in langs:
        if quota[lang] == 0:
            donor = max(langs, key=lambda x: quota[x])
            if quota[donor] > 1:
                quota[donor] -= 1
                quota[lang] = 1

    return quota


def sample_queries(
    queries: list[dict], rng: random.Random, count: int = QUERIES_TO_SAMPLE
) -> list[dict]:
    """Pick `count` queries respecting the proportional language quota."""
    by_language: dict[str, list[dict]] = {}
    for query in queries:
        by_language.setdefault(query["language"], []).append(query)

    quota = language_quota(
        {lang: len(pool) for lang, pool in by_language.items()}, count
    )

    picked: list[dict] = []
    for lang, n in quota.items():
        pool = by_language[lang]
        picked.extend(rng.sample(pool, min(n, len(pool))))

    # Backfill if a language's pool was smaller than its quota.
    if len(picked) < count:
        chosen = {q["id"] for q in picked}
        remaining = [q for q in queries if q["id"] not in chosen]
        rng.shuffle(remaining)
        picked.extend(remaining[: count - len(picked)])

    return picked[:count]


def band_targets(n_pairs: int) -> dict[str, int]:
    """Split n_pairs across the rank bands as evenly as possible.

    25 pairs becomes 9/8/8 rather than silently dropping a band.
    """
    base, remainder = divmod(n_pairs, len(BANDS))
    return {
        name: base + (1 if i < remainder else 0) for i, (name, _) in enumerate(BANDS)
    }


def _pick_from_band(
    results: list[RetrievedPassage], band: tuple[int, int], rng: random.Random
) -> RetrievedPassage | None:
    low, high = band
    in_band = [r for r in results if low <= r.rank <= high]
    return rng.choice(in_band) if in_band else None


def sample_pairs(
    n_pairs: int,
    seed: int,
    languages: list[str],
    batch: int,
    exclude_query_ids: set[str] | None = None,
    exclude_passage_ids: set[int] | None = None,
) -> list[dict]:
    """Draw n_pairs (query, passage) pairs, band-stratified, from unused queries.

    One pair per band per query, so the query count is the largest band target.
    Bands are then trimmed to their exact targets — 25 pairs comes out 9/8/8
    rather than dropping a band or over-filling one.
    """
    rng = random.Random(seed)
    targets = band_targets(n_pairs)
    seen_passages = set(exclude_passage_ids or ())

    queries = sample_queries(
        load_queries(languages, exclude_query_ids), rng, max(targets.values())
    )

    sampled_counts: dict[str, int] = {}
    for query in queries:
        sampled_counts[query["language"]] = sampled_counts.get(query["language"], 0) + 1
    print(
        "Sampled queries by language: "
        + ", ".join(f"{lang} {n}" for lang, n in sorted(sampled_counts.items()))
    )

    per_band: dict[str, list[dict]] = {name: [] for name, _ in BANDS}
    with db_connection() as conn:
        for i, query in enumerate(queries, 1):
            print(
                f"retrieving [{i}/{len(queries)}] {query['id']}: {query['query']!r}",
                flush=True,
            )
            results, detected = retrieve_vibe(
                conn, query["query"], top_k=RETRIEVAL_DEPTH
            )
            usable = [r for r in results if r.passage_id not in seen_passages]
            for band_name, band in BANDS:
                chosen = _pick_from_band(usable, band, rng)
                if chosen is None:
                    print(
                        f"  no unused results in band {band_name} {band} — skipped",
                        file=sys.stderr,
                    )
                    continue
                seen_passages.add(chosen.passage_id)  # no passage twice across the set
                per_band[band_name].append(
                    {
                        "batch": batch,
                        "query_id": query["id"],
                        "query": query["query"],
                        "language": query["language"],
                        "detected_language": detected,
                        "band": band_name,
                        "rank": chosen.rank,
                        "passage_id": chosen.passage_id,
                        "artist": chosen.artist,
                        "title": chosen.title,
                        "passage_language": chosen.language,
                        "passage_text": chosen.passage_text,
                        "human_grade": None,
                    }
                )

    pairs: list[dict] = []
    for band_name, target in targets.items():
        drawn = per_band[band_name]
        if len(drawn) > target:
            drawn = rng.sample(drawn, target)
        elif len(drawn) < target:
            print(
                f"warning: band {band_name} has {len(drawn)} pairs, wanted {target}",
                file=sys.stderr,
            )
        pairs.extend(drawn)

    rng.shuffle(pairs)  # so the grader can't infer rank from position
    if len(pairs) < n_pairs:
        print(f"warning: built {len(pairs)} pairs, wanted {n_pairs}", file=sys.stderr)
    return pairs


def build_calibration_set(seed: int, languages: list[str]) -> list[dict]:
    return sample_pairs(TARGET_PAIRS, seed, languages, batch=1)


def save_calibration(pairs: list[dict], meta: dict) -> None:
    ensure_eval_dir()
    payload = {
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": meta["seed"],
        # Recorded so the kappa is never read as covering languages it never saw.
        "languages": meta["languages"],
        # One entry per --add batch, so a held-out split is reproducible.
        "batches": meta["batches"],
        "config": config_description(),
        "bands": {name: list(band) for name, band in BANDS},
        "pairs": pairs,
    }
    tmp = CALIBRATION_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CALIBRATION_PATH)  # atomic: an interrupt can't lose graded work


def load_calibration() -> tuple[list[dict], dict]:
    payload = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    pairs = payload["pairs"]
    for pair in pairs:  # sets written before batching existed are all batch 1
        pair.setdefault("batch", 1)

    seed = payload.get("seed", 0)
    meta = {
        "seed": seed,
        "languages": payload.get("languages", DEFAULT_LANGUAGES),
        "batches": payload.get("batches")
        or [
            {
                "batch": 1,
                "seed": seed,
                "n": len(pairs),
                "sampled_at": payload.get("sampled_at"),
            }
        ],
    }
    return pairs, meta


# --------------------------------------------------------------------------- #
# Grading CLI
# --------------------------------------------------------------------------- #


def _render(pair: dict, index: int, total: int) -> None:
    lang = pair.get("passage_language") or "??"
    print("\n" + "─" * 72)
    print(f'[{index}/{total}]  Query: "{pair["query"]}"')
    print(f"        Song: {pair['artist']} - {pair['title']} [{lang}]")
    print()
    for line in pair["passage_text"].strip().splitlines()[:12]:
        print(f"        {line}")
    print()
    print(f"        {GRADE_LEGEND}")
    print("        u = undo    q = save and quit")
    print("        > ", end="", flush=True)


def grade(pairs: list[dict], meta: dict) -> None:
    total = len(pairs)
    history: list[int] = []
    index = 0

    while index < total:
        if pairs[index]["human_grade"] is not None:
            index += 1
            continue

        _render(pairs[index], index + 1, total)
        try:
            key = getch().lower()
        except (KeyboardInterrupt, EOFError):
            print("\ninterrupted — progress saved")
            break

        if key in "0123" and key != "":
            print(key)
            pairs[index]["human_grade"] = int(key)
            history.append(index)
            # save after each entry; survives interruption
            save_calibration(pairs, meta)
            index += 1
        elif key == "u":
            if not history:
                print("u\n        nothing to undo")
                continue
            index = history.pop()
            pairs[index]["human_grade"] = None
            save_calibration(pairs, meta)
            print("u\n        undone")
        elif key == "q":
            print("q\n        saving and quitting")
            break
        else:
            print(f"{key}\n        unknown key — use 0 / 1 / 2 / 3 / u / q")


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade the calibration set by hand")
    parser.add_argument(
        "--resample",
        action="store_true",
        help="Rebuild the pair set from retrieval, discarding existing grades",
    )
    parser.add_argument(
        "--add",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Append N new pairs as a fresh batch, drawn from queries the set has "
            "never used. Existing pairs and grades are untouched."
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Sampling seed (default: 42)"
    )
    parser.add_argument(
        "--languages",
        default=",".join(DEFAULT_LANGUAGES),
        help=(
            "Comma-separated query languages to sample from "
            f"(default: {','.join(DEFAULT_LANGUAGES)}). Languages you cannot read "
            "should be left out — the kappa then covers only what is listed here."
        ),
    )
    parser.add_argument(
        "--sample-only",
        action="store_true",
        help="Build or extend the pair set and exit, without entering the grading UI",
    )
    args = parser.parse_args()

    languages = [lang.strip() for lang in args.languages.split(",") if lang.strip()]
    if not languages:
        print("--languages must name at least one language", file=sys.stderr)
        sys.exit(1)

    if args.add and args.resample:
        print("--add and --resample are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if args.add:
        if not CALIBRATION_PATH.exists():
            print(
                f"--add needs an existing {CALIBRATION_PATH.name}; build one first.",
                file=sys.stderr,
            )
            sys.exit(1)
        pairs, meta = load_calibration()
        batch = max(p["batch"] for p in pairs) + 1
        # A fresh seed per batch, so batch 2 does not replay batch 1's draws.
        batch_seed = args.seed + batch - 1
        print(
            f"Extending calibration set: {len(pairs)} existing pairs, "
            f"adding {args.add} as batch {batch}"
        )
        new_pairs = sample_pairs(
            args.add,
            batch_seed,
            languages,
            batch=batch,
            exclude_query_ids={p["query_id"] for p in pairs},
            exclude_passage_ids={p["passage_id"] for p in pairs},
        )
        meta["batches"].append(
            {
                "batch": batch,
                "seed": batch_seed,
                "n": len(new_pairs),
                "languages": languages,
                "sampled_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        pairs.extend(new_pairs)  # appended, so grading resumes at the new batch
        save_calibration(pairs, meta)
        print(f"\nAdded {len(new_pairs)} pairs -> {CALIBRATION_PATH}\n")
    elif args.resample or not CALIBRATION_PATH.exists():
        if CALIBRATION_PATH.exists() and args.resample:
            graded = sum(
                1 for p in load_calibration()[0] if p["human_grade"] is not None
            )
            if graded:
                print(f"--resample will discard {graded} existing grades.")
                print("Type 'yes' to continue: ", end="", flush=True)
                if input().strip().lower() != "yes":
                    print("aborted")
                    sys.exit(1)
        pairs = build_calibration_set(args.seed, languages)
        meta = {
            "seed": args.seed,
            "languages": languages,
            "batches": [
                {
                    "batch": 1,
                    "seed": args.seed,
                    "n": len(pairs),
                    "languages": languages,
                    "sampled_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            ],
        }
        save_calibration(pairs, meta)
        print(f"\nBuilt {len(pairs)} pairs -> {CALIBRATION_PATH}\n")
    else:
        pairs, meta = load_calibration()
        print(
            f"Resuming existing calibration set "
            f"(languages: {','.join(meta['languages'])})"
        )

    if args.sample_only:
        print("--sample-only: not entering the grading UI")
    else:
        grade(pairs, meta)

    graded = sum(1 for p in pairs if p["human_grade"] is not None)
    print("\n" + "─" * 72)
    print(f"Graded {graded}/{len(pairs)} pairs -> {CALIBRATION_PATH}")
    for entry in meta["batches"]:
        in_batch = [p for p in pairs if p["batch"] == entry["batch"]]
        done = sum(1 for p in in_batch if p["human_grade"] is not None)
        print(f"  batch {entry['batch']}: {done}/{len(in_batch)} graded")
    if graded < len(pairs):
        print("\nResume with: python -m src.eval.grade_calibration")
    else:
        print("\nNext: python -m src.eval.calibrate")


if __name__ == "__main__":
    main()
