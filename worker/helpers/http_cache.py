"""Shared HTTP + TTL caching support for worker tools.

The cache is Redis-backed (via the shared async `redis_service` client), so
frequent external API calls (World Bank, BLS, Frankfurter, ...) are only made
once per TTL window per unique request. Cache keys are namespaced
`tool_cache:{sha256}` and stored with `setex` so they expire automatically.
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx
from redis_service import redis

TOOL_CACHE_NAMESPACE = "tool_cache"

# The async client keeps the worker event loop happy — no sync client (and no
# `asyncio.to_thread`) is needed. Retries are handled per-request below.
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(15.0, connect=6.0),
    follow_redirects=True,
    headers={
        "Accept": "application/json",
        "User-Agent": "KapexAI/0.1 (+https://github.com/kapexai)",
    },
)


class ToolServiceError(RuntimeError):
    """Raised when an external service cannot satisfy a request."""


async def cached_json(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    ttl_seconds: int = 900,
) -> Any:
    """Fetch JSON through the shared async connection pool and TTL cache."""
    key = _cache_key(method, url, params, json_body, headers)
    cached = await redis.get(_redis_key(key))
    if cached is not None:
        return json.loads(cached)

    response: httpx.Response | None = None
    try:
        for attempt in range(3):
            response = await _client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
            )
            if response.status_code not in {429, 502, 503, 504}:
                break
            if attempt < 2:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    min(float(retry_after), 3.0)
                    if retry_after and retry_after.replace(".", "", 1).isdigit()
                    else 0.5 * (2**attempt)
                )
                await asyncio.sleep(delay)
        if response is None:
            raise ToolServiceError(f"Request to {url} produced no response")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolServiceError(f"Request to {url} failed: {exc}") from exc

    await redis.setex(
        _redis_key(key),
        ttl_seconds,
        json.dumps(payload, default=str, separators=(",", ":")),
    )
    return payload


def _cache_key(
    method: str,
    url: str,
    params: Mapping[str, Any] | None,
    body: Any,
    headers: Mapping[str, str] | None,
) -> str:
    material = json.dumps(
        {
            "method": method.upper(),
            "url": url,
            "params": params,
            "body": body,
            "headers": headers,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _redis_key(cache_key: str) -> str:
    return f"{TOOL_CACHE_NAMESPACE}:{cache_key}"
