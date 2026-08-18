"""Hand-computed checks for src/eval/metrics.py.

Every expected value below is derived by hand in the comments, not captured
from a previous run. The whole eval rests on these functions being right.
"""

from __future__ import annotations

import math

import pytest

from src.eval import metrics

# --------------------------------------------------------------------------- #
# Synthetic 3-query eval, worked out by hand
# --------------------------------------------------------------------------- #

# Q1: a 3 at rank 1, a 2 at rank 3
Q1 = [3, 0, 2, 0, 0, 0, 0, 0, 0, 0]
# Q2: first relevant (grade 2) at rank 3
Q2 = [0, 1, 2, 3, 0, 0, 0, 0, 0, 0]
# Q3: nothing relevant at all
Q3 = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]

SYNTHETIC = [Q1, Q2, Q3]


def test_recall_at_k():
    # Q1 has a relevant result at rank 1; Q2's first is at rank 3; Q3 has none.
    assert metrics.recall_at_k(Q1, 1) == 1.0
    assert metrics.recall_at_k(Q2, 1) == 0.0
    assert metrics.recall_at_k(Q2, 5) == 1.0
    assert metrics.recall_at_k(Q3, 10) == 0.0
    # grade 1 is below the >= 2 threshold
    assert metrics.recall_at_k([1, 1, 1], 3) == 0.0


def test_reciprocal_rank():
    assert metrics.reciprocal_rank(Q1) == 1.0
    assert metrics.reciprocal_rank(Q2) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank(Q3) == 0.0


def test_dcg_hand_computed():
    # Q1: (2^3-1)/log2(2) + 0 + (2^2-1)/log2(4) = 7/1 + 3/2 = 8.5
    assert metrics.dcg(Q1, 10) == pytest.approx(8.5)


def test_ndcg_hand_computed():
    # Q1 DCG  = 7/log2(2) + 3/log2(4)         = 7 + 1.5          = 8.5
    #    IDCG = 7/log2(2) + 3/log2(3)         = 7 + 1.8927892607 = 8.8927892607
    #    NDCG = 8.5 / 8.8927892607                               = 0.9558305893
    assert metrics.ndcg_at_k(Q1, 10) == pytest.approx(0.9558305893, abs=1e-9)

    # Q2 DCG  = 1/log2(3) + 3/log2(4) + 7/log2(5)
    #         = 0.6309297536 + 1.5 + 3.0147359064               = 5.1456656601
    #    IDCG = 7/log2(2) + 3/log2(3) + 1/log2(4)
    #         = 7 + 1.8927892607 + 0.5                          = 9.3927892607
    #    NDCG = 5.1456656601 / 9.3927892607                     = 0.5478314819
    assert metrics.ndcg_at_k(Q2, 10) == pytest.approx(0.5478314819, abs=1e-9)

    # Q3: no relevant results anywhere -> IDCG 0 -> defined as 0.0
    assert metrics.ndcg_at_k(Q3, 10) == 0.0


def test_summarize_hand_computed():
    summary = metrics.summarize(SYNTHETIC)
    assert summary.n == 3
    # recall@1: only Q1 -> 1/3
    assert summary.recall_at_1 == pytest.approx(1 / 3)
    # recall@5 and @10: Q1 and Q2 -> 2/3
    assert summary.recall_at_5 == pytest.approx(2 / 3)
    assert summary.recall_at_10 == pytest.approx(2 / 3)
    # MRR: (1 + 1/3 + 0) / 3 = 0.444444
    assert summary.mrr == pytest.approx(4 / 9)
    # NDCG@10: (0.9558305893 + 0.5478314819 + 0) / 3 = 0.5012206904
    assert summary.ndcg_at_10 == pytest.approx(0.5012206904, abs=1e-9)


def test_ndcg_is_one_for_perfectly_ordered_results():
    assert metrics.ndcg_at_k([3, 3, 2, 1, 0], 10) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Agreement statistics, worked out by hand
# --------------------------------------------------------------------------- #

# Judge agrees exactly or is one grade low on 3 of 8 pairs.
A = [3, 3, 2, 2, 1, 1, 0, 0]
B = [3, 2, 2, 1, 1, 0, 0, 0]


def test_kappa_perfect_agreement():
    assert metrics.quadratic_weighted_kappa(A, A) == pytest.approx(1.0)


def test_kappa_maximal_disagreement():
    # a = [3,3,0,0], b = [0,0,3,3]
    #   sum(w*O) = 1.0 * 0.5 + 1.0 * 0.5 = 1.0
    #   marginals both [0.5, 0, 0, 0.5] -> sum(w*E) = 0.25 + 0.25 = 0.5
    #   kappa = 1 - 1.0/0.5 = -1.0
    assert metrics.quadratic_weighted_kappa(
        [3, 3, 0, 0], [0, 0, 3, 3]
    ) == pytest.approx(-1.0)


def test_kappa_hand_computed():
    # n = 8, weights (i-j)^2 / 9.
    #   observed disagreements: three pairs off by exactly 1 -> sum (i-j)^2 = 3
    #   sum(w*O) = 3 / (9*8)                                  = 0.0416667
    #   marginals: A = [1/4]*4;  B = [3/8, 2/8, 2/8, 1/8]
    #   sum_i (i-j)^2 = 14, 6, 6, 14 for j = 0,1,2,3
    #   sum(w*E) = (0.25/9) * (3*14 + 2*6 + 2*6 + 1*14)/8 = (0.25/9)*10 = 0.277778
    #   kappa = 1 - 0.0416667 / 0.277778 = 1 - 0.15 = 0.85
    assert metrics.quadratic_weighted_kappa(A, B) == pytest.approx(0.85)


def test_kappa_undefined_when_a_rater_is_constant():
    assert math.isnan(metrics.quadratic_weighted_kappa([2, 2, 2, 2], [2, 2, 2, 2]))


def test_kappa_penalises_far_errors_more_than_near_ones():
    near = metrics.quadratic_weighted_kappa([3, 2, 1, 0], [2, 2, 1, 0])
    far = metrics.quadratic_weighted_kappa([3, 2, 1, 0], [0, 2, 1, 0])
    assert near > far


def test_spearman_hand_computed():
    # average ranks: A -> [7.5,7.5,5.5,5.5,3.5,3.5,1.5,1.5]
    #                B -> [8,6.5,6.5,4.5,4.5,2,2,2]
    # cov = 36.0, var_A = 40, var_B = 39
    # rho = 36 / sqrt(1560) = 0.911465
    assert metrics.spearman(A, B) == pytest.approx(0.911465, abs=1e-6)


def test_spearman_monotone_extremes():
    assert metrics.spearman([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)
    assert metrics.spearman([0, 1, 2, 3], [3, 2, 1, 0]) == pytest.approx(-1.0)


def test_exact_and_within_one_rates():
    # A vs B: exact on 5 of 8; all 8 within 1
    assert metrics.exact_match_rate(A, B) == pytest.approx(5 / 8)
    assert metrics.within_one_rate(A, B) == pytest.approx(1.0)


def test_confusion_matrix_hand_computed():
    matrix = metrics.confusion_matrix(A, B)
    # rows = human/A grade, cols = judge/B grade
    assert matrix == [
        [2, 0, 0, 0],  # A=0 -> B=0 twice
        [1, 1, 0, 0],  # A=1 -> B=0 once, B=1 once
        [0, 1, 1, 0],  # A=2 -> B=1 once, B=2 once
        [0, 0, 1, 1],  # A=3 -> B=2 once, B=3 once
    ]
    assert sum(sum(row) for row in matrix) == len(A)


def test_kappa_reading_thresholds():
    assert "strong" in metrics.kappa_reading(0.78)
    assert "acceptable" in metrics.kappa_reading(0.65)
    assert "weak" in metrics.kappa_reading(0.50)
    assert "unusable" in metrics.kappa_reading(0.20)


def test_paired_inputs_must_match():
    with pytest.raises(ValueError):
        metrics.quadratic_weighted_kappa([1, 2], [1])
