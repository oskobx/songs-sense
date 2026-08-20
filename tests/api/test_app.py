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


def test_vibe_search_retries_once_on_dead_connection(monkeypatch):
    """A session dropped while idle must cost a retry, not a 500.

    psycopg only marks a connection broken after an operation on it fails, so
    the first query after a drop always raises. Neon reclaims idle sessions, so
    this is the normal steady-state failure, not an edge case.
    """
    import psycopg

    class FakeConn:
        def execute(self, *args, **kwargs):
            class R:
                def fetchall(self):
                    return [(7, "Artist", "Title", 1999, "a passage")]

            return R()

    attempts = {"n": 0}
    closed = {"n": 0}

    def flaky_search(conn, query, lang, top_k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg.OperationalError(
                "terminating connection due to administrator command"
            )
        return [(7, 0.9)]

    monkeypatch.setattr(api, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(api, "semantic_search", flaky_search)
    monkeypatch.setattr(
        api, "close_connection", lambda: closed.__setitem__("n", closed["n"] + 1)
    )

    detected, results = api.vibe_search("late night drive", 10)

    assert attempts["n"] == 2, "should have retried exactly once"
    assert closed["n"] == 1, "should have dropped the dead connection before retrying"
    assert [r.artist for r in results] == ["Artist"]


def test_vibe_search_gives_up_after_second_failure(monkeypatch):
    import psycopg

    def always_dead(conn, query, lang, top_k):
        raise psycopg.OperationalError("connection is dead")

    monkeypatch.setattr(api, "get_connection", lambda: object())
    monkeypatch.setattr(api, "semantic_search", always_dead)
    monkeypatch.setattr(api, "close_connection", lambda: None)

    with pytest.raises(psycopg.OperationalError):
        api.vibe_search("late night drive", 10)
