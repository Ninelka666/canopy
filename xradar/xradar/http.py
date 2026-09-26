"""Minimal HTTP helper: stdlib only, with retries and rate-limit respect."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

USER_AGENT = "xradar/0.1 (+personal research tool)"


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


class RateLimited(HttpError):
    def __init__(self, status: int, body: str, url: str, retry_after: float):
        super().__init__(status, body, url)
        self.retry_after = retry_after


def get_json(
    url: str,
    params: Optional[Dict[str, object]] = None,
    headers: Optional[Dict[str, str]] = None,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 2.0,
    sleep=time.sleep,
) -> dict:
    """GET a JSON document, retrying transient failures with backoff.

    A 4xx other than 429 is not retried: a bad query or a bad key will not fix
    itself, and retrying it on a metered API just spends money.
    """
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True
        )
        url = f"{url}?{query}"
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request_headers.update(headers or {})

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429 or exc.code >= 500:
                wait = _retry_after(exc.headers, backoff * (2 ** attempt))
                last_error = (
                    RateLimited(exc.code, body, url, wait) if exc.code == 429
                    else HttpError(exc.code, body, url)
                )
                if attempt < retries:
                    sleep(min(wait, 120.0))
                    continue
                raise last_error
            raise HttpError(exc.code, body, url) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                sleep(backoff * (2 ** attempt))
                continue
            raise
    raise last_error or RuntimeError("unreachable")


def _retry_after(headers, fallback: float) -> float:
    for key in ("retry-after", "x-rate-limit-reset"):
        raw = headers.get(key) if headers else None
        if not raw:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # x-rate-limit-reset is an absolute epoch; retry-after is a duration.
        if value > 1e9:
            value = value - time.time()
        if 0 < value < 900:
            return value
    return fallback


def post_json(url: str, payload: dict, *, timeout: float = 20.0,
              retries: int = 2, backoff: float = 1.5, sleep=time.sleep) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429 or exc.code >= 500:
                wait = _retry_after(exc.headers, backoff * (2 ** attempt))
                last_error = HttpError(exc.code, body, url)
                if attempt < retries:
                    sleep(min(wait, 60.0))
                    continue
            raise HttpError(exc.code, body, url) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                sleep(backoff * (2 ** attempt))
                continue
            raise
    raise last_error or RuntimeError("unreachable")
