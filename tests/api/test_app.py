"""API tests. Retrieval and model loading are stubbed — no DB, no downloads."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import app as api


@pytest.fixture
def no_models(monkeypatch):
    """Record which embedding models the lifespan handler would load."""
    loaded: list[str] = []
    monkeypatch.setattr(api, "_ensure_bge_base", lambda: loaded.append(api.BGE_BASE))
    monkeypatch.setattr(api, "_ensure_bge_m3", lambda: loaded.append(api.BGE_M3))
    # Startup also pings the database; keep the test off the network.
    monkeypatch.setattr(
        api, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("no db"))
    )
    return loaded


@pytest.fixture
def stub_search(monkeypatch):
    """Stub vibe_search, recording the k it was handed after clamping."""
    seen: dict[str, int] = {}

    def fake(query: str, k: int):
        seen["k"] = k
        results = [
            api.SearchResult(
                rank=i,
                artist=f"Artist {i}",
                title=f"Title {i}",
                year=2000 + i,
                passage=f"passage {i}",
                score=1.0 - i / 100,
            )
            for i in range(1, k + 1)
        ]
        return "en", results

    monkeypatch.setattr(api, "vibe_search", fake)
    return seen


def test_search_returns_expected_shape(no_models, stub_search):
    with TestClient(api.app) as client:
        response = client.post("/search/vibe", json={"query": "late night drive"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"query", "detected_language", "results"}
    assert body["query"] == "late night drive"
    assert body["detected_language"] == "en"
    assert len(body["results"]) == api.K_DEFAULT

    first = body["results"][0]
    assert set(first) == {"rank", "artist", "title", "year", "passage", "score"}
    assert first["rank"] == 1
    assert [r["rank"] for r in body["results"]] == list(range(1, 11))


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(1, 1), (10, 10), (25, 25), (100, 25), (0, 1), (-5, 1)],
)
def test_k_is_clamped(no_models, stub_search, requested, expected):
    with TestClient(api.app) as client:
        response = client.post("/search/vibe", json={"query": "x", "k": requested})

    assert response.status_code == 200
    assert stub_search["k"] == expected
    assert len(response.json()["results"]) == expected


def test_empty_query_is_rejected(no_models, stub_search):
    with TestClient(api.app) as client:
        for query in ("", "   "):
            response = client.post("/search/vibe", json={"query": query})
            assert response.status_code == 400
    assert "k" not in stub_search  # retrieval never ran


def test_multilingual_enabled_loads_both_models(no_models, monkeypatch):
    monkeypatch.setenv("EMBED_MULTILINGUAL", "true")
    with TestClient(api.app) as client:
        models = client.get("/health").json()["models"]

    assert no_models == [api.BGE_BASE, api.BGE_M3]
    assert models == [api.BGE_BASE, api.BGE_M3]


def test_multilingual_disabled_skips_bge_m3(no_models, monkeypatch):
    monkeypatch.setenv("EMBED_MULTILINGUAL", "false")
    with TestClient(api.app) as client:
        body = client.get("/health").json()

    assert no_models == [api.BGE_BASE]
    assert api.BGE_M3 not in body["models"]
    assert body["status"] == "ok"


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_multilingual_flag_accepts_common_falsey_spellings(monkeypatch, value):
    monkeypatch.setenv("EMBED_MULTILINGUAL", value)
    assert api.embed_multilingual_enabled() is False


def test_multilingual_defaults_to_on(monkeypatch):
    monkeypatch.delenv("EMBED_MULTILINGUAL", raising=False)
    assert api.embed_multilingual_enabled() is True


@pytest.mark.parametrize(
    ("detected", "multilingual", "expected"),
    [
        ("en", True, "en"),
        ("pl", True, "pl"),
        ("en", False, "en"),
        # English-only mode must not send pl/de/es to bge-m3, and must not
        # boost them toward English either.
        ("pl", False, None),
        ("de", False, None),
        ("es", False, None),
        (None, True, None),
    ],
)
def test_routing_language(monkeypatch, detected, multilingual, expected):
    monkeypatch.setenv("EMBED_MULTILINGUAL", "true" if multilingual else "false")
    assert api._routing_language(detected) == expected
