"""Canonical locations for eval artifacts, so every module agrees on them."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = PROJECT_ROOT / "data" / "eval"

RAW_QUERIES_PATH = EVAL_DIR / "vibe_queries_raw.json"
QUERIES_PATH = EVAL_DIR / "vibe_queries.json"
CALIBRATION_PATH = EVAL_DIR / "calibration.json"
JUDGE_CACHE_PATH = EVAL_DIR / "judge_cache.json"


def ensure_eval_dir() -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    return EVAL_DIR


def results_path(timestamp: str) -> Path:
    return EVAL_DIR / f"results_{timestamp}.json"
