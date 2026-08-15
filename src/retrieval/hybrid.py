"""Reciprocal Rank Fusion (RRF) combiner for hybrid retrieval."""

from __future__ import annotations


def rrf_combine(
    rankings: list[list[tuple[int, float]]],
    k: int = 60,
    top_n: int = 10,
) -> list[tuple[int, float]]:
    """Combine multiple ranked lists via RRF.

    Each element of rankings is a list of (passage_id, score) sorted best-first.
    Scores are ignored — only rank position matters.
    Returns [(passage_id, rrf_score), ...] sorted by rrf_score desc.
    """
    scores: dict[int, float] = {}
    for ranked_list in rankings:
        for rank_0, (passage_id, _) in enumerate(ranked_list):
            rank_1 = rank_0 + 1  # 1-indexed
            scores[passage_id] = scores.get(passage_id, 0.0) + 1.0 / (k + rank_1)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
