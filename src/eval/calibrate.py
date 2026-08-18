"""Measure judge-vs-human agreement on the calibration set.

Runs the judge over the same pairs the human graded and reports quadratic-
weighted kappa (primary), Spearman, exact-match and within-1 rates, and the 4x4
confusion matrix.

Without this number the absolute metrics mean nothing — the eval has no
ground-truth labels, so the judge is the only source of truth and the kappa is
the only evidence it can be trusted.

Usage:
    python -m src.eval.calibrate
    python -m src.eval.calibrate --versions v1,v2,v3
    python -m src.eval.calibrate --self-consistency 10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from src.eval import metrics
from src.eval.groq_client import DEFAULT_MODEL
from src.eval.judge import (
    PROMPT_TEMPLATES,
    PROMPT_VERSION,
    JudgeCache,
    JudgePair,
    judge_all,
    judge_pair,
    prompt_signature,
)
from src.eval.grade_calibration import GRADE_FIELD, REGRADE_FIELD
from src.eval.paths import EVAL_DIR, CALIBRATION_PATH, ensure_eval_dir

REPORT_PATH = EVAL_DIR / "calibration_report.json"
GRADE_LABELS = ["0", "1", "2", "3"]


def load_graded_pairs(field: str = GRADE_FIELD) -> tuple[list[dict], list[str]]:
    """Return (graded pairs, the query languages the calibration set covers).

    Pairs carry a "batch" stamp; sets written before batching existed are batch 1.
    """
    if not CALIBRATION_PATH.exists():
        print(
            f"{CALIBRATION_PATH} not found. Run:\n  python -m src.eval.grade_calibration",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    graded = [p for p in payload["pairs"] if p.get(field) is not None]
    ungraded = len(payload["pairs"]) - len(graded)
    if not graded:
        print("No human grades yet — run grade_calibration first.", file=sys.stderr)
        sys.exit(1)
    if ungraded:
        print(f"note: {ungraded} pairs are still ungraded and are excluded\n")

    for pair in graded:
        pair.setdefault("batch", 1)

    languages = payload.get("languages") or sorted({p["language"] for p in graded})
    return graded, languages


def to_judge_pair(pair: dict) -> JudgePair:
    return JudgePair(
        query_id=pair["query_id"],
        query=pair["query"],
        passage_id=pair["passage_id"],
        artist=pair["artist"],
        title=pair["title"],
        passage_text=pair["passage_text"],
        language=pair.get("passage_language"),
    )


def print_confusion(matrix: list[list[int]]) -> None:
    print("  Confusion matrix (rows = human, cols = judge)")
    print("           " + "".join(f"{label:>6}" for label in GRADE_LABELS) + "   total")
    for i, row in enumerate(matrix):
        print(
            f"  human {i}  "
            + "".join(f"{count:>6}" for count in row)
            + f"   {sum(row):>5}"
        )
    totals = [
        sum(matrix[i][j] for i in range(len(matrix))) for j in range(len(GRADE_LABELS))
    ]
    print(
        "  total    "
        + "".join(f"{count:>6}" for count in totals)
        + f"   {sum(totals):>5}"
    )


def print_disagreements(pairs: list[dict], human: list[int], judged: list[int]) -> None:
    rows = [
        (abs(h - j), h, j, p)
        for p, h, j in zip(pairs, human, judged, strict=True)
        if abs(h - j) >= 2
    ]
    if not rows:
        print("\n  No disagreements of 2 or more grades.")
        return
    print(f"\n  Disagreements of 2+ grades ({len(rows)}) — these are the ones to read:")
    for _, h, j, pair in sorted(rows, reverse=True, key=lambda r: r[0]):
        snippet = pair["passage_text"].strip().splitlines()[0][:60]
        print(
            f"    human {h} vs judge {j}  [{pair['language']}] "
            f"{pair['query'][:40]!r}\n"
            f"        {pair['artist']} - {pair['title']}: {snippet}"
        )


def self_consistency(pairs: list[dict], n: int, model: str) -> float | None:
    """Re-judge n pairs without the cache and report how often the grade repeats.

    A judge that disagrees with itself puts a ceiling on the kappa it can reach
    against a human, so this separates "judge is wrong" from "judge is noisy".
    """
    sample = pairs[:n]
    if not sample:
        return None
    print(f"\nSelf-consistency check: re-judging {len(sample)} pairs (uncached)...")
    repeats = 0
    for pair in sample:
        judge_pair_obj = to_judge_pair(pair)
        first = judge_pair(judge_pair_obj, model=model)
        second = judge_pair(judge_pair_obj, model=model)
        same = first == second
        repeats += int(same)
        print(
            f"  {pair['query_id']}|{pair['passage_id']}: {first} then {second} "
            f"{'(same)' if same else '(DIFFERENT)'}"
        )
    return repeats / len(sample)


def subsets(pairs: list[dict]) -> list[tuple[str, list[int]]]:
    """(label, indices) for each batch, plus the pooled set.

    Batches exist so the judge prompt can be tuned on one and reported on
    another. Pooling them is fine for reference but is not a held-out number.
    """
    batches = sorted({p["batch"] for p in pairs})
    out = [
        (f"batch {b}", [i for i, p in enumerate(pairs) if p["batch"] == b])
        for b in batches
    ]
    if len(batches) > 1:
        out.append(("pooled", list(range(len(pairs)))))
    return out


def print_grade_distribution(pairs: list[dict]) -> None:
    print("Human grade distribution")
    print("            " + "".join(f"{g:>6}" for g in GRADE_LABELS) + "     n   mean")
    for label, idx in subsets(pairs):
        grades = [pairs[i]["human_grade"] for i in idx]
        counts = "".join(f"{grades.count(g):>6}" for g in range(4))
        mean = sum(grades) / len(grades)
        print(f"  {label:<9}" + counts + f"  {len(grades):>4}   {mean:.2f}")


def agreement(human: list[int], judged: list[int]) -> dict:
    return {
        "n": len(human),
        "quadratic_weighted_kappa": metrics.quadratic_weighted_kappa(human, judged),
        "spearman": metrics.spearman(human, judged),
        "exact_match_rate": metrics.exact_match_rate(human, judged),
        "within_one_rate": metrics.within_one_rate(human, judged),
        "confusion_matrix": metrics.confusion_matrix(human, judged),
        "judge_relevant": sum(1 for g in judged if g >= 2),
        "human_relevant": sum(1 for g in human if g >= 2),
    }


def print_intra_rater(pairs: list[dict]) -> dict | None:
    """Agreement between the grader's two independent passes over the same pairs.

    This is the ceiling on judge-vs-human kappa. If the same person, shown the
    same pair twice, agrees with themselves at kappa 0.6, no judge can be shown
    to agree with "the human" above roughly that - the disagreement is in the
    target, not the model.
    """
    both = [
        p
        for p in pairs
        if p.get(GRADE_FIELD) is not None and p.get(REGRADE_FIELD) is not None
    ]
    if not both:
        return None

    round1 = [p[GRADE_FIELD] for p in both]
    round2 = [p[REGRADE_FIELD] for p in both]
    stats = agreement(round1, round2)

    print("\n" + "=" * 72)
    print(f"=== Intra-rater agreement (round 1 vs blind round 2), n={len(both)} ===\n")
    # Deliberately not metrics.kappa_reading() - that table reads a judge's
    # trustworthiness, and this number is the grader against themselves.
    print(f"  Quadratic-weighted kappa   {stats['quadratic_weighted_kappa']:.3f}")
    print(f"  Spearman correlation       {stats['spearman']:.3f}")
    print(f"  Exact match                {stats['exact_match_rate']:.3f}")
    print(f"  Within 1 grade             {stats['within_one_rate']:.3f}")
    print()
    print("  Confusion matrix (rows = round 1, cols = round 2)")
    print("           " + "".join(f"{g:>6}" for g in GRADE_LABELS) + "   total")
    for i, row in enumerate(stats["confusion_matrix"]):
        print(f"  round1 {i} " + "".join(f"{c:>6}" for c in row) + f"   {sum(row):>5}")
    totals = [
        sum(stats["confusion_matrix"][i][j] for i in range(4))
        for j in range(len(GRADE_LABELS))
    ]
    print("  total    " + "".join(f"{c:>6}" for c in totals) + f"   {sum(totals):>5}")
    print(
        f"\n  'relevant' (>=2): round 1 {stats['human_relevant']}/{len(both)}, "
        f"round 2 {stats['judge_relevant']}/{len(both)}"
    )
    print(
        "\n  This is the ceiling. A judge cannot be shown to agree with this grader\n"
        "  much above this number, however good the prompt gets."
    )
    return stats


def print_regrade_comparison(
    pairs: list[dict], versions: list[str], results: dict, model: str
) -> dict:
    """Re-run each version against round-2 grades on the re-graded pairs only."""
    both_idx = [
        i
        for i, p in enumerate(pairs)
        if p.get(GRADE_FIELD) is not None and p.get(REGRADE_FIELD) is not None
    ]
    round1 = [pairs[i][GRADE_FIELD] for i in both_idx]
    round2 = [pairs[i][REGRADE_FIELD] for i in both_idx]

    print("\n" + "=" * 72)
    print(f"=== Judge kappa under each grading round, n={len(both_idx)} ===\n")
    print("  version    round 1     round 2       delta")
    out: dict[str, dict] = {}
    for version in versions:
        judged = [results[version]["judge_grades"][i] for i in both_idx]
        k1 = metrics.quadratic_weighted_kappa(round1, judged)
        k2 = metrics.quadratic_weighted_kappa(round2, judged)
        print(f"  {version:<9}{k1:>9.3f}{k2:>11.3f}{k2 - k1:>+12.3f}")
        out[version] = {"round1": k1, "round2": k2, "delta": k2 - k1}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Judge-vs-human agreement metrics")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--versions",
        default=None,
        help=(
            "Comma-separated prompt versions to measure "
            f"(default: the active one, {PROMPT_VERSION}). "
            f"Available: {','.join(sorted(PROMPT_TEMPLATES))}"
        ),
    )
    parser.add_argument(
        "--self-consistency",
        type=int,
        default=0,
        metavar="N",
        help="Re-judge N pairs twice (uncached) to measure judge self-agreement",
    )
    args = parser.parse_args()

    versions = (
        [v.strip() for v in args.versions.split(",") if v.strip()]
        if args.versions
        else [PROMPT_VERSION]
    )
    unknown = [v for v in versions if v not in PROMPT_TEMPLATES]
    if unknown:
        print(
            f"unknown prompt version(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(PROMPT_TEMPLATES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    pairs, languages = load_graded_pairs()
    human = [p["human_grade"] for p in pairs]
    judge_pairs_list = [to_judge_pair(p) for p in pairs]

    results: dict[str, dict] = {}
    for version in versions:
        signature = prompt_signature(args.model, version)
        cache = JudgeCache(signature=signature)
        print(f"Judging {len(pairs)} pairs with {args.model} prompt {version}...")
        graded = judge_all(
            judge_pairs_list, cache, args.model, progress=False, version=version
        )
        judged = [graded[p.cache_key] for p in judge_pairs_list]
        print(f"  {cache.hits} cached, {cache.misses} new API calls")
        results[version] = {
            "signature": signature,
            "judge_grades": judged,
            "subsets": {
                label: agreement([human[i] for i in idx], [judged[i] for i in idx])
                for label, idx in subsets(pairs)
            },
        }

    by_language: dict[str, int] = {}
    for pair in pairs:
        by_language[pair["language"]] = by_language.get(pair["language"], 0) + 1

    print("\n" + "=" * 72)
    print(f"=== Judge calibration — {args.model} ===")
    print(
        f"n = {len(pairs)} human-graded pairs "
        + "("
        + ", ".join(f"{k} {v}" for k, v in sorted(by_language.items()))
        + ")"
    )
    print(f"Covers query languages: {', '.join(languages)}\n")

    print_grade_distribution(pairs)

    labels = [label for label, _ in subsets(pairs)]
    print("\nQuadratic-weighted kappa")
    print("  version   " + "".join(f"{lab:>12}" for lab in labels))
    for version in versions:
        row = "".join(
            f"{results[version]['subsets'][lab]['quadratic_weighted_kappa']:>12.3f}"
            for lab in labels
        )
        print(f"  {version:<9}" + row)
    print(
        "  "
        + " " * 9
        + "".join(
            f"{'n=' + str(results[versions[0]]['subsets'][lab]['n']):>12}"
            for lab in labels
        )
    )

    for stat, title in (
        ("spearman", "Spearman"),
        ("exact_match_rate", "Exact match"),
        ("within_one_rate", "Within 1 grade"),
    ):
        print(f"\n{title}")
        print("  version   " + "".join(f"{lab:>12}" for lab in labels))
        for version in versions:
            row = "".join(
                f"{results[version]['subsets'][lab][stat]:>12.3f}" for lab in labels
            )
            print(f"  {version:<9}" + row)

    print("\nJudge 'relevant' count (grade >= 2) vs human")
    print("  version   " + "".join(f"{lab:>12}" for lab in labels))
    for version in versions:
        row = "".join(
            f"{str(results[version]['subsets'][lab]['judge_relevant']) + '/' + str(results[version]['subsets'][lab]['n']):>12}"
            for lab in labels
        )
        print(f"  {version:<9}" + row)
    print(
        "  human    "
        + "".join(
            f"{str(results[versions[0]]['subsets'][lab]['human_relevant']) + '/' + str(results[versions[0]]['subsets'][lab]['n']):>12}"
            for lab in labels
        )
    )

    for version in versions:
        for label in labels:
            print(f"\n--- {version}, {label} ---")
            print_confusion(results[version]["subsets"][label]["confusion_matrix"])

    active = results[versions[-1]]
    print()
    print_disagreements(pairs, human, active["judge_grades"])

    intra = print_intra_rater(pairs)
    regrade_comparison = (
        print_regrade_comparison(pairs, versions, results, args.model)
        if intra
        else None
    )

    print(
        f"\n  Kappa on a batch of n={len(pairs)} has a wide confidence interval. "
        "State that in the\n  writeup rather than over-claiming."
    )
    print(
        f"  This kappa covers {', '.join(languages)} queries only. Metrics for other "
        "languages\n  rest on a judge that was never checked against a human on them."
    )
    if len(labels) > 1:
        print(
            "  Report the held-out batch number, not the pooled one, for any version "
            "whose\n  prompt was tuned against an earlier batch."
        )

    consistency = None
    if args.self_consistency:
        consistency = self_consistency(pairs, args.self_consistency, args.model)
        print(f"\n  Judge self-agreement: {consistency:.3f}")

    ensure_eval_dir()
    REPORT_PATH.write_text(
        json.dumps(
            {
                "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "model": args.model,
                "versions": versions,
                "active_version": PROMPT_VERSION,
                "n": len(pairs),
                "languages": languages,
                "n_by_language": by_language,
                "human_grades": human,
                "batches": [p["batch"] for p in pairs],
                "self_agreement": consistency,
                "intra_rater": intra,
                "regrade_comparison": regrade_comparison,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
