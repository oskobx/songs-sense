# songs-sense

A multi-mode music search platform: vibe search, fuzzy song retrieval, and lyric-to-text matching over a curated corpus of popular songs. Built as a portfolio project demonstrating RAG, retrieval evaluation, and embedding fine-tuning. Work in progress.

## Phase 3b: Vibe Search evaluation

A reproducible benchmark for Vibe Search, so later changes (reranker, fine-tuned
embeddings, chunk tuning) can be measured rather than guessed at.

### How it works

114 curated vibe queries (81 en, 19 pl, 9 de, 5 es) each retrieve the top 10
passages. An LLM judge (`openai/gpt-oss-120b` via Groq) grades every
(query, passage) pair 0–3 on how well the passage delivers the feeling the query
describes. Judgments are cached on `(query_id, passage_id)`, namespaced by model
and prompt version, so re-running against unchanged retrieval is nearly free.

The judge is worth only as much as its agreement with a human, so 55 pairs were
hand-graded: sampled across three rank bands (1–3, 4–10, 15–30) so the set
contains low grades, and restricted to en/pl since the grader cannot reliably
judge de or es. The 55 split into two batches drawn from disjoint queries —
batch 1 (30 pairs) drove three rounds of judge-prompt iteration, batch 2 (25)
was held out and scored once. Batch 1 was then re-graded blind (same pairs,
fresh order, original grades hidden), and the judge re-scored pairs twice.

| Comparison | Statistic | Value | n |
|---|---|---|---|
| Judge vs human, held-out batch 2 | quadratic-weighted kappa | 0.46 | 25 |
| Judge vs human, tuning batch 1 (re-graded) | quadratic-weighted kappa | 0.34 | 30 |
| Human vs self, blind re-grade | quadratic-weighted kappa | 0.42 | 30 |
| Judge vs self, repeated calls | exact-repeat rate | 0.74–0.90 | 10 |

The judge agrees with the grader about as closely as the grader agrees with
themselves, so the ceiling here is human labelling noise rather than the model.
Read the kappa as near the floor of what the task can resolve; further prompt
tuning against a target this noisy would be fitting to noise.

### Baseline

Judge prompt v3, semantic-only retrieval, language boost +0.1, no reranker,
8-line chunks, over ~9.4k songs and 86k passages.

| Slice | n | recall@1 | recall@5 | recall@10 | MRR | NDCG@10 |
|---|---|---|---|---|---|---|
| Overall | 114 | 0.50 | 0.82 | 0.89 | 0.63 | 0.80 |
| en | 81 | 0.48 | 0.84 | 0.90 | 0.63 | 0.81 |
| pl | 19 | 0.53 | 0.74 | 0.84 | 0.62 | 0.78 |
| de | 9 | 0.44 | 0.67 | 0.78 | 0.57 | 0.73 |
| es | 5 | 0.80 | 1.00 | 1.00 | 0.90 | 0.88 |

The eval found one concrete bug. The language detector was built with only
English, Polish and German, so every Spanish query was detected as `en` or `pl`,
routed to the English-only embedding model, and boosted toward the wrong
language. Adding Spanish to the detector and to the multilingual routing set
moved es from MRR 0.46 / NDCG 0.71 / recall@1 0.20 to 0.90 / 0.88 / 0.80, and
overall MRR from 0.61 to 0.63. Detection now matches the declared language on
all 114 queries.

### Limitations

- The judge sits at the label noise floor, so absolute numbers are provisional.
- Calibration covers en/pl only; the de and es rows rest on a judge never
  checked against a human in those languages.
- n = 9 (de) and n = 5 (es) are too small to read as language comparisons.
- NDCG@10 uses each query's observed grades as its ideal ranking, the usual
  practical approximation. It flatters queries whose best available result is
  mediocre, which is why NDCG 0.80 sits alongside recall@1 0.50.
- An earlier judge version cached a 0 for unparseable responses, indistinguishable
  from a genuine "not relevant". All 233 affected entries were re-judged; 93
  changed against a 26% baseline churn from judge nondeterminism, so roughly 30
  were real repairs. Individual entries cannot be attributed.
- Covers Vibe Search only. Find the Song and Lyric Twin are unevaluated.

### Running it

```bash
python -m src.eval.generate_vibe_queries            # LLM-generated candidates
python -m src.eval.curate_queries                   # keep/delete CLI
python -m src.eval.grade_calibration                # sample + grade 30 pairs
python -m src.eval.grade_calibration --add 25       # held-out batch
python -m src.eval.grade_calibration --regrade 1    # blind re-grade
python -m src.eval.calibrate --versions v3 --self-consistency 10
python -m src.eval.run_vibe_eval --note "what changed"
```

Results land in `data/eval/results_<timestamp>.json` with per-query detail and
the retrieval config. Needs `GROQ_KEY` in `.env` and a populated local Postgres.
