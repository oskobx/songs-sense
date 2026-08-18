"""Keep/delete CLI over generated candidates, producing the final eval set.

Reads data/eval/vibe_queries_raw.json, writes data/eval/vibe_queries.json.

Delete anything that is a near-duplicate of an earlier query, a song title in
disguise, too vague to have any answer ("music"), or too specific to be a vibe
("songs mentioning Tuesday").

Decisions are saved after every keypress to a progress file, so quitting
halfway and resuming loses nothing.

Usage:
    python -m src.eval.curate_queries
    python -m src.eval.curate_queries --restart

Keys:
    k  keep      d  delete      u  undo previous      q  save and quit
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from src.eval.paths import EVAL_DIR, QUERIES_PATH, RAW_QUERIES_PATH, ensure_eval_dir
from src.utils.keypress import getch

PROGRESS_PATH = EVAL_DIR / "curation_progress.json"

TARGET_DISTRIBUTION: dict[str, int] = {"en": 70, "pl": 15, "de": 10, "es": 5}


def load_candidates() -> list[dict]:
    if not RAW_QUERIES_PATH.exists():
        print(
            f"{RAW_QUERIES_PATH} not found. Run:\n"
            "  python -m src.eval.generate_vibe_queries",
            file=sys.stderr,
        )
        sys.exit(1)
    payload = json.loads(RAW_QUERIES_PATH.read_text(encoding="utf-8"))
    return payload["candidates"]


def load_progress() -> dict[str, str]:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(
            "warning: curation progress file is corrupt, starting over", file=sys.stderr
        )
        return {}


def save_progress(decisions: dict[str, str]) -> None:
    ensure_eval_dir()
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def _kept_counts(candidates: list[dict], decisions: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        if decisions.get(candidate["raw_id"]) == "k":
            lang = candidate["language"]
            counts[lang] = counts.get(lang, 0) + 1
    return counts


def _render(
    candidate: dict,
    index: int,
    total: int,
    kept: int,
    lang_counts: dict[str, int],
) -> None:
    lang = candidate["language"]
    target = TARGET_DISTRIBUTION.get(lang, 0)
    have = lang_counts.get(lang, 0)
    print("\n" + "─" * 72)
    print(
        f"[{index}/{total}]  kept {kept}   "
        f"{lang} {have}/{target}   category: {candidate['category']}"
    )
    print()
    print(f"    {candidate['query']}")
    print()
    print("    k = keep    d = delete    u = undo    q = save and quit")
    print("    > ", end="", flush=True)


def write_eval_set(candidates: list[dict], decisions: dict[str, str]) -> list[dict]:
    """Write vibe_queries.json with contiguous vibe_NNN ids in candidate order.

    Note: ids are positional, so re-curating from scratch can shift them, which
    invalidates judge-cache entries keyed on (query_id, passage_id). Curate once
    and keep the file — that is the intended workflow.
    """
    kept = [c for c in candidates if decisions.get(c["raw_id"]) == "k"]
    queries = [
        {
            "id": f"vibe_{i:03d}",
            "query": c["query"],
            "language": c["language"],
            "category": c["category"],
        }
        for i, c in enumerate(kept, 1)
    ]

    counts: dict[str, int] = {}
    for query in queries:
        counts[query["language"]] = counts.get(query["language"], 0) + 1

    ensure_eval_dir()
    payload = {
        "curated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": RAW_QUERIES_PATH.name,
        "counts_by_language": counts,
        "queries": queries,
    }
    QUERIES_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return queries


def curate(candidates: list[dict], decisions: dict[str, str]) -> dict[str, str]:
    total = len(candidates)
    history: list[str] = []  # raw_ids decided this session, for undo
    index = 0

    while index < total:
        candidate = candidates[index]
        if candidate["raw_id"] in decisions:  # already decided in an earlier session
            index += 1
            continue

        kept_counts = _kept_counts(candidates, decisions)
        kept_total = sum(kept_counts.values())
        _render(candidate, index + 1, total, kept_total, kept_counts)

        try:
            key = getch().lower()
        except (KeyboardInterrupt, EOFError):
            print("\ninterrupted — progress saved")
            break

        if key in ("k", "d"):
            print(key)
            decisions[candidate["raw_id"]] = key
            history.append(candidate["raw_id"])
            save_progress(decisions)
            index += 1
        elif key == "u":
            if not history:
                print("u\n    nothing to undo")
                continue
            last = history.pop()
            decisions.pop(last, None)
            save_progress(decisions)
            index = next(i for i, c in enumerate(candidates) if c["raw_id"] == last)
            print("u\n    undone")
        elif key == "q":
            print("q\n    saving and quitting")
            break
        else:
            print(f"{key}\n    unknown key — use k / d / u / q")

    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description="Curate generated vibe queries")
    parser.add_argument(
        "--restart", action="store_true", help="Discard saved progress and start over"
    )
    args = parser.parse_args()

    candidates = load_candidates()
    decisions = {} if args.restart else load_progress()
    if args.restart and PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()

    decisions = curate(candidates, decisions)
    queries = write_eval_set(candidates, decisions)

    reviewed = len(decisions)
    print("\n" + "─" * 72)
    print(f"Reviewed {reviewed}/{len(candidates)} candidates, kept {len(queries)}")
    for lang, target in TARGET_DISTRIBUTION.items():
        have = sum(1 for q in queries if q["language"] == lang)
        marker = "ok" if have == target else f"target {target}"
        print(f"  {lang}: {have:>3}  ({marker})")
    print(f"\nWrote {QUERIES_PATH}")
    if reviewed < len(candidates):
        print("Resume with: python -m src.eval.curate_queries")


if __name__ == "__main__":
    main()
