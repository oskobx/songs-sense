"""Retrieval metrics (recall@k, MRR, NDCG) and rater-agreement statistics.

All functions here are pure: they take grade lists and return numbers. No I/O,
no LLM calls. That makes them testable against hand-computed values, which is
the whole point — every other module in src/eval/ trusts these.

Grade scale is the judge scale from the spec: 0 (not relevant) .. 3 (excellent).
"Relevant" for the binary metrics (recall@k, MRR) means grade >= 2.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

RELEVANT_THRESHOLD = 2
N_GRADES = 4  # grades 0..3

# --------------------------------------------------------------------------- #
# Per-query retrieval metrics
# --------------------------------------------------------------------------- #


def recall_at_k(
    grades: Sequence[int], k: int, threshold: int = RELEVANT_THRESHOLD
) -> float:
    """1.0 if any of the top-k results has grade >= threshold, else 0.0.

    Per-query this is binary; the reported recall@k is its mean over queries.
    """
    return 1.0 if any(g >= threshold for g in grades[:k]) else 0.0


def reciprocal_rank(
    grades: Sequence[int], threshold: int = RELEVANT_THRESHOLD
) -> float:
    """1 / (1-indexed rank of the first result with grade >= threshold), else 0.0."""
    for rank_0, grade in enumerate(grades):
        if grade >= threshold:
            return 1.0 / (rank_0 + 1)
    return 0.0


def dcg(grades: Sequence[int], k: int) -> float:
    """Exponential-gain DCG@k: sum (2^grade - 1) / log2(rank + 1)."""
    return sum(
        (2**grade - 1) / math.log2(rank_0 + 2)
        for rank_0, grade in enumerate(grades[:k])
    )


def ndcg_at_k(grades: Sequence[int], k: int = 10) -> float:
    """NDCG@k with IDCG taken over the *observed* grades for this query.

    That is the standard practical approximation when there is no exhaustive
    relevance labelling of the corpus — worth stating in the writeup, since it
    makes NDCG optimistic relative to a fully-labelled ideal ranking.

    A query with no relevant results at all (all grades 0) scores 0.0.
    """
    ideal = sorted(grades[:k], reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0.0:
        return 0.0
    return dcg(grades, k) / idcg


# --------------------------------------------------------------------------- #
# Aggregation across queries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MetricSummary:
    n: int
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(per_query_grades: Sequence[Sequence[int]]) -> MetricSummary:
    """Aggregate per-query graded result lists into the headline metrics."""
    return MetricSummary(
        n=len(per_query_grades),
        recall_at_1=_mean([recall_at_k(g, 1) for g in per_query_grades]),
        recall_at_5=_mean([recall_at_k(g, 5) for g in per_query_grades]),
        recall_at_10=_mean([recall_at_k(g, 10) for g in per_query_grades]),
        mrr=_mean([reciprocal_rank(g) for g in per_query_grades]),
        ndcg_at_10=_mean([ndcg_at_k(g, 10) for g in per_query_grades]),
    )


# --------------------------------------------------------------------------- #
# Rater agreement (judge vs human)
# --------------------------------------------------------------------------- #


def confusion_matrix(
    a: Sequence[int], b: Sequence[int], n_classes: int = N_GRADES
) -> list[list[int]]:
    """Counts matrix, rows indexed by `a` grade, columns by `b` grade."""
    _check_paired(a, b)
    matrix = [[0] * n_classes for _ in range(n_classes)]
    for grade_a, grade_b in zip(a, b, strict=True):
        matrix[grade_a][grade_b] += 1
    return matrix


def quadratic_weighted_kappa(
    a: Sequence[int], b: Sequence[int], n_classes: int = N_GRADES
) -> float:
    """Cohen's kappa with quadratic weights w_ij = (i - j)^2 / (n_classes - 1)^2.

    kappa = 1 - sum(w * O) / sum(w * E), where O is the joint distribution of the
    two raters and E is the outer product of their marginals. Quadratic weights
    are the right choice here because the grades are ordinal: confusing 3 with 2
    should cost far less than confusing 3 with 0.

    Returns NaN when expected disagreement is 0 (e.g. one rater used a single
    grade throughout) — kappa is genuinely undefined there, and returning 1.0
    would silently claim perfect agreement.
    """
    _check_paired(a, b)
    n = len(a)
    denom = (n_classes - 1) ** 2

    observed = confusion_matrix(a, b, n_classes)
    hist_a = [0] * n_classes
    hist_b = [0] * n_classes
    for grade_a, grade_b in zip(a, b, strict=True):
        hist_a[grade_a] += 1
        hist_b[grade_b] += 1

    num = 0.0
    den = 0.0
    for i in range(n_classes):
        for j in range(n_classes):
            weight = ((i - j) ** 2) / denom
            num += weight * (observed[i][j] / n)
            den += weight * (hist_a[i] / n) * (hist_b[j] / n)

    if den == 0.0:
        return float("nan")
    return 1.0 - num / den


def _average_ranks(values: Sequence[float]) -> list[float]:
    """1-indexed ranks, ties sharing the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j + 2) / 2  # mean of 1-indexed ranks i+1 .. j+1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Spearman rank correlation (Pearson on average-tied ranks).

    Returns NaN if either input has zero rank variance.
    """
    _check_paired(a, b)
    ranks_a = _average_ranks(a)
    ranks_b = _average_ranks(b)
    mean_a = _mean(ranks_a)
    mean_b = _mean(ranks_b)

    cov = sum(
        (x - mean_a) * (y - mean_b) for x, y in zip(ranks_a, ranks_b, strict=True)
    )
    var_a = sum((x - mean_a) ** 2 for x in ranks_a)
    var_b = sum((y - mean_b) ** 2 for y in ranks_b)

    if var_a == 0.0 or var_b == 0.0:
        return float("nan")
    return cov / math.sqrt(var_a * var_b)


def exact_match_rate(a: Sequence[int], b: Sequence[int]) -> float:
    _check_paired(a, b)
    return _mean([1.0 if x == y else 0.0 for x, y in zip(a, b, strict=True)])


def within_one_rate(a: Sequence[int], b: Sequence[int]) -> float:
    _check_paired(a, b)
    return _mean([1.0 if abs(x - y) <= 1 else 0.0 for x, y in zip(a, b, strict=True)])


def kappa_reading(kappa: float) -> str:
    """Spec's interpretation table, so every report reads the number the same way."""
    if math.isnan(kappa):
        return "undefined (no expected disagreement)"
    if kappa > 0.75:
        return "strong — trust the judge"
    if kappa >= 0.60:
        return "acceptable — report kappa alongside metrics"
    if kappa >= 0.40:
        return "weak — add few-shot examples to the judge prompt, re-measure"
    return "unusable — do not report absolute numbers"


def _check_paired(a: Sequence, b: Sequence) -> None:
    if len(a) != len(b):
        raise ValueError(
            f"paired sequences must be equal length, got {len(a)} and {len(b)}"
        )
    if not a:
        raise ValueError("paired sequences must be non-empty")
