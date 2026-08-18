"""Main Vibe Search eval harness.

Runs every curated query through vibe-mode retrieval, grades the top 10 with the
calibrated judge, computes recall@k / MRR / NDCG@10 overall and by language, and
writes a timestamped results file.

The retrieval config is recorded in the results file. Comparing runs across
changes is guesswork without it.

Usage:
    python -m src.eval.run_vibe_eval
    python -m src.eval.run_vibe_eval --limit 5           # smoke run
    python -m src.eval.run_vibe_eval --note "with reranker"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from src.eval import metrics
from src.eval.groq_client import DEFAULT_MODEL
from src.eval.judge import JudgeCache, JudgePair, judge_all, prompt_signature
from src.eval.paths import EVAL_DIR, QUERIES_PATH, ensure_eval_dir, results_path
from src.eval.retrieval_runner import config_description, db_connection, retrieve_vibe

CALIBRATION_REPORT_PATH = EVAL_DIR / "calibration_report.json"
LANGUAGE_ORDER = ["en", "pl", "de", "es"]
TOP_K = 10


def load_queries(limit: int | None) -> list[dict]:
    if not QUERIES_PATH.exists():
        print(
            f"{QUERIES_PATH} not found. Run:\n"
            "  python -m src.eval.generate_vibe_queries\n"
            "  python -m src.eval.curate_queries",
            file=sys.stderr,
        )
        sys.exit(1)
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]
    return queries[:limit] if limit else queries


def load_judge_credibility() -> dict | None:
    """The kappa line for the header. Absent is fine, but worth saying so loudly."""
    if not CALIBRATION_REPORT_PATH.exists():
        return None
    try:
        return json.loads(CALIBRATION_REPORT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def retrieve_all(queries: list[dict], top_k: int) -> list[dict]:
    """Retrieve for every query first, so the DB and models are done with before judging."""
    per_query: list[dict] = []
    with db_connection() as conn:
        for i, query in enumerate(queries, 1):
            results, detected = retrieve_vibe(conn, query["query"], top_k=top_k)
            print(
                f"retrieved [{i}/{len(queries)}] {query['id']} "
                f"({query['language']}, detected {detected}): {len(results)} results",
                flush=True,
            )
            per_query.append(
                {
                    "id": query["id"],
                    "query": query["query"],
                    "language": query["language"],
                    "detected_language": detected,
                    "results": results,
                }
            )
    return per_query


def judge_all_results(
    per_query: list[dict], model: str
) -> tuple[dict[str, int], JudgeCache]:
    pairs = [
        JudgePair(
            query_id=entry["id"],
            query=entry["query"],
            passage_id=result.passage_id,
            artist=result.artist,
            title=result.title,
            passage_text=result.passage_text,
            language=result.language,
        )
        for entry in per_query
        for result in entry["results"]
    ]
    cache = JudgeCache(signature=prompt_signature(model))
    print(f"\nJudging {len(pairs)} (query, passage) pairs...")
    # judge_all keys by cache_key; dict(judge_pairs(...)) would key by JudgePair object.
    graded = judge_all(pairs, cache, model, progress=False)
    print(f"  {cache.hits} cached, {cache.misses} new API calls")
    return graded, cache


def build_per_query_detail(per_query: list[dict], graded: dict[str, int]) -> list[dict]:
    detail: list[dict] = []
    for entry in per_query:
        grades = [graded[f"{entry['id']}|{r.passage_id}"] for r in entry["results"]]
        detail.append(
            {
                "id": entry["id"],
                "query": entry["query"],
                "language": entry["language"],
                "detected_language": entry["detected_language"],
                "grades": grades,
                "recall_at_1": metrics.recall_at_k(grades, 1),
                "recall_at_5": metrics.recall_at_k(grades, 5),
                "recall_at_10": metrics.recall_at_k(grades, 10),
                "reciprocal_rank": metrics.reciprocal_rank(grades),
                "ndcg_at_10": metrics.ndcg_at_k(grades, 10),
                "results": [
                    {
                        "rank": r.rank,
                        "passage_id": r.passage_id,
                        "score": r.score,
                        "artist": r.artist,
                        "title": r.title,
                        "language": r.language,
                        "grade": grade,
                    }
                    for r, grade in zip(entry["results"], grades, strict=True)
                ],
            }
        )
    return detail


def print_summary(
    detail: list[dict],
    overall: metrics.MetricSummary,
    by_language: dict[str, metrics.MetricSummary],
    config: str,
    model: str,
    credibility: dict | None,
    timestamp: str,
) -> None:
    print("\n" + "=" * 72)
    print(f"=== Vibe Search Eval — {timestamp} ===")
    print(f"Config: {config}")
    if credibility:
        kappa = credibility["quadratic_weighted_kappa"]
        covered = credibility.get("languages") or []
        scope = f", {'/'.join(covered)} only" if covered else ""
        print(
            f"Judge: {model} (weighted kappa = {kappa:.2f}, "
            f"n={credibility['n']}{scope})"
        )
        evaluated = {d["language"] for d in detail}
        uncovered = sorted(evaluated - set(covered)) if covered else []
        if uncovered:
            print(
                f"  Note: {', '.join(uncovered)} rows below are judged by a model "
                "never calibrated\n  against a human on those languages."
            )
        if kappa < 0.60:
            print(
                "  WARNING: kappa below 0.60 — do not report these absolute numbers "
                "without\n  fixing the judge prompt first."
            )
    else:
        print(
            f"Judge: {model} (UNCALIBRATED — run src.eval.calibrate before reporting)"
        )

    print(f"\nOverall (n={overall.n})")
    print(f"  recall@1   {overall.recall_at_1:.2f}")
    print(f"  recall@5   {overall.recall_at_5:.2f}")
    print(f"  recall@10  {overall.recall_at_10:.2f}")
    print(f"  MRR        {overall.mrr:.2f}")
    print(f"  NDCG@10    {overall.ndcg_at_10:.2f}")

    print("\nBy language")
    for lang in LANGUAGE_ORDER:
        summary = by_language.get(lang)
        if summary is None:
            continue
        print(
            f"  {lang} ({summary.n:>3})   MRR {summary.mrr:.2f}   "
            f"NDCG {summary.ndcg_at_10:.2f}   recall@10 {summary.recall_at_10:.2f}"
        )
    small = [lang for lang, s in by_language.items() if s.n < 10]
    if small:
        print(f"  (n < 10 for {', '.join(small)} — do not over-read those rows)")

    mismatches = [d for d in detail if d["detected_language"] != d["language"]]
    if mismatches:
        print(
            f"\nLanguage detection disagreed with the declared language on "
            f"{len(mismatches)}/{len(detail)} queries:"
        )
        for d in mismatches[:5]:
            print(
                f"  {d['id']}: declared {d['language']}, detected "
                f"{d['detected_language']} — {d['query'][:50]!r}"
            )
        if len(mismatches) > 5:
            print(
                f"  ... and {len(mismatches) - 5} more (full list in the results file)"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Vibe Search eval")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Judge model")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument(
        "--limit", type=int, default=None, help="Only run the first N queries"
    )
    parser.add_argument(
        "--note", default=None, help="Appended to the recorded config line"
    )
    args = parser.parse_args()

    queries = load_queries(args.limit)
    config = config_description(args.note)
    print(f"Config: {config}")
    print(f"Queries: {len(queries)}\n")

    per_query = retrieve_all(queries, args.top_k)
    graded, cache = judge_all_results(per_query, args.model)
    detail = build_per_query_detail(per_query, graded)

    overall = metrics.summarize([d["grades"] for d in detail])
    by_language: dict[str, metrics.MetricSummary] = {}
    for lang in LANGUAGE_ORDER:
        grades = [d["grades"] for d in detail if d["language"] == lang]
        if grades:
            by_language[lang] = metrics.summarize(grades)

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    credibility = load_judge_credibility()
    print_summary(
        detail, overall, by_language, config, args.model, credibility, timestamp
    )

    ensure_eval_dir()
    out_path = results_path(datetime.now(UTC).strftime("%Y%m%d_%H%M%S"))
    out_path.write_text(
        json.dumps(
            {
                "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "config": config,
                "judge_model": args.model,
                "judge_prompt_signature": prompt_signature(args.model),
                "judge_calibration": credibility,
                "top_k": args.top_k,
                "n_queries": len(detail),
                "cache_hits": cache.hits,
                "cache_misses": cache.misses,
                "overall": overall.as_dict(),
                "by_language": {k: v.as_dict() for k, v in by_language.items()},
                "per_query": detail,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
