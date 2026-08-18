"""Minimal Groq chat-completions client for the eval tooling.

Uses httpx directly rather than the `groq` SDK — httpx is already a project
dependency and the surface we need is one POST. Groq speaks the OpenAI
chat-completions schema, so this is ~40 lines and no new dependency.

Handles the two things that actually bite on the free tier: 30 RPM rate limits
(client-side spacing + honouring Retry-After on 429) and transient 5xx.
"""

from __future__ import annotations

import os
import random
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Free tier is ~30 requests/minute. Space calls slightly wider than 60/30 = 2.0s
# so a burst of retries doesn't push us over.
DEFAULT_RPM = 28

_last_call_at: float = 0.0


class GroqError(RuntimeError):
    pass


# .env.example documents GROQ_API_KEY but the actual .env uses GROQ_KEY.
# Accept either rather than force a rename that would break other scripts.
KEY_ENV_VARS = ("GROQ_API_KEY", "GROQ_KEY")


def _api_key() -> str:
    for name in KEY_ENV_VARS:
        key = os.environ.get(name, "").strip()
        if key:
            return key
    raise GroqError(
        f"No Groq key found. Set one of {' or '.join(KEY_ENV_VARS)} in .env "
        "(get one at https://console.groq.com/keys)."
    )


def _throttle(rpm: int) -> None:
    """Sleep just enough to keep the global call rate under `rpm`."""
    global _last_call_at
    if rpm <= 0:
        return
    min_interval = 60.0 / rpm
    elapsed = time.monotonic() - _last_call_at
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call_at = time.monotonic()


def chat(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    system: str | None = None,
    temperature: float = 0.0,
    max_completion_tokens: int = 1024,
    reasoning_effort: str | None = "low",
    rpm: int = DEFAULT_RPM,
    max_retries: int = 4,
    timeout: float = 120.0,
) -> str:
    """Send one chat completion and return the assistant's text content.

    `reasoning_effort` is a gpt-oss-specific knob on Groq; "low" keeps the
    hidden reasoning short, which matters when a judgment is a single digit and
    we make 1000 of them. Set to None for models that reject the field.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort

    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }

    last_error: str = ""
    for attempt in range(max_retries):
        _throttle(rpm)
        try:
            response = httpx.post(
                GROQ_URL, json=payload, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            last_error = f"transport error: {exc}"
            time.sleep(_backoff(attempt))
            continue

        if response.status_code == 200:
            data = response.json()
            try:
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError) as exc:
                raise GroqError(f"unexpected response shape: {data}") from exc

        if response.status_code == 429:
            wait = _retry_after(response) or _backoff(attempt)
            last_error = f"429 rate limited (waiting {wait:.1f}s)"
            time.sleep(wait)
            continue

        if response.status_code >= 500:
            last_error = f"{response.status_code}: {response.text[:200]}"
            time.sleep(_backoff(attempt))
            continue

        # 4xx other than 429 won't fix itself.
        raise GroqError(f"Groq returned {response.status_code}: {response.text[:500]}")

    raise GroqError(
        f"Groq call failed after {max_retries} attempts. Last error: {last_error}"
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(2.0 * (2**attempt), 30.0) + random.uniform(0, 0.5)
