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
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Free tier is ~30 requests/minute. Space calls slightly wider than 60/30 = 2.0s
# so a burst of retries doesn't push us over.
DEFAULT_RPM = 28

# Hard ceiling on our own computed backoff, jitter included. A server-supplied
# Retry-After is honoured verbatim and is deliberately NOT capped: Groq knows
# when the quota window reopens, and retrying earlier just burns an attempt on
# another 429.
MAX_BACKOFF_SECONDS = 30.0

_last_call_at: float = 0.0

# Optional floor on the gap between requests, set by callers that need to stay
# under a tokens-per-minute ceiling rather than a requests-per-minute one.
_min_interval: float = 0.0

# Rate-limit headers are printed once per process, on the first response.
_rate_limits_reported: bool = False


def set_min_interval(seconds: float) -> None:
    """Force at least `seconds` between requests, on top of the RPM spacing.

    Groq caps this model at 8,000 tokens/minute, which binds well before the
    requests/minute limit does: a judge call is roughly 400-700 tokens, so ~12
    requests/minute is the real ceiling. Only actual HTTP calls are throttled,
    so a cache hit costs nothing.
    """
    global _min_interval
    _min_interval = max(0.0, seconds)


def _report_rate_limits(response: httpx.Response) -> None:
    """Print Groq's quota headers once, so a long run's budget is visible up front."""
    global _rate_limits_reported
    if _rate_limits_reported:
        return
    _rate_limits_reported = True
    fields = [
        ("requests remaining", "x-ratelimit-remaining-requests"),
        ("requests limit", "x-ratelimit-limit-requests"),
        ("requests reset", "x-ratelimit-reset-requests"),
        ("tokens remaining", "x-ratelimit-remaining-tokens"),
        ("tokens limit", "x-ratelimit-limit-tokens"),
        ("tokens reset", "x-ratelimit-reset-tokens"),
    ]
    present = [(label, response.headers.get(h)) for label, h in fields]
    if not any(v for _, v in present):
        print("groq: no rate-limit headers on this response", file=sys.stderr)
        return
    print("groq rate limits (first response of this run):", file=sys.stderr)
    for label, value in present:
        if value is not None:
            print(f"  {label:<20} {value}", file=sys.stderr)
    sys.stderr.flush()


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
    if rpm <= 0 and _min_interval <= 0:
        return
    if rpm <= 0:
        min_interval = _min_interval
        elapsed = time.monotonic() - _last_call_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _last_call_at = time.monotonic()
        return
    min_interval = max(60.0 / rpm, _min_interval)
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
            _report_rate_limits(response)
            data = response.json()
            try:
                choice = data["choices"][0]
                content = choice["message"]["content"] or ""
            except (KeyError, IndexError) as exc:
                raise GroqError(f"unexpected response shape: {data}") from exc
            # gpt-oss spends reasoning tokens from the same budget, so a tight
            # max_completion_tokens can truncate before the answer is emitted.
            # Silent truncation would show up as a mystery grade of 0.
            if choice.get("finish_reason") == "length":
                print(
                    f"groq: response hit max_completion_tokens "
                    f"({max_completion_tokens}); content may be truncated: "
                    f"{content[:60]!r}",
                    file=sys.stderr,
                    flush=True,
                )
            return content

        if response.status_code == 429:
            _report_rate_limits(
                response
            )  # a 429 is the most informative first response
            retry_after = _retry_after(response)
            wait = retry_after if retry_after is not None else _backoff(attempt)
            source = "server Retry-After" if retry_after is not None else "backoff"
            last_error = f"429 rate limited (waited {wait:.1f}s)"
            print(
                f"groq: rate limited, sleeping {wait:.1f}s [{source}] "
                f"(attempt {attempt + 1}/{max_retries})",
                file=sys.stderr,
                flush=True,
            )
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
    """Exponential backoff with jitter, hard-capped at MAX_BACKOFF_SECONDS."""
    return min(2.0 * (2**attempt) + random.uniform(0, 0.5), MAX_BACKOFF_SECONDS)
