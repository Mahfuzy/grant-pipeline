"""Polite HTTP client (SPEC §9): robots.txt, per-domain rate limit, retries, conditional GETs."""

import asyncio
import email.utils
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)

from fundscout.fetch.ratelimit import DomainRateLimiter
from fundscout.fetch.robots import RobotsChecker

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, url: str, message: str):
        super().__init__(f"{message} ({url})")
        self.url = url


class RobotsDisallowed(FetchError):
    def __init__(self, url: str):
        super().__init__(url, "disallowed by robots.txt")


class HttpStatusError(FetchError):
    def __init__(self, url: str, status: int):
        super().__init__(url, f"HTTP {status}")
        self.status = status


class RetryableError(FetchError):
    """Transient failure (429, 5xx, network error, block page). Retried with backoff."""

    def __init__(self, url: str, message: str, retry_after: float | None = None):
        super().__init__(url, message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class FetchRequest:
    url: str
    method: str = "GET"
    json_body: Any = None
    params: Mapping[str, str] = field(default_factory=dict)


@dataclass
class FetchedDocument:
    url: str
    status_code: int
    content: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    fetched_at: datetime

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        rate_limiter: DomainRateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 4,
        backoff_base: float = 5.0,
        max_backoff: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        check_robots: bool = True,
    ):
        self.user_agent = user_agent
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = rate_limiter or DomainRateLimiter(sleep=sleep)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._robots = RobotsChecker(self._fetch_robots, user_agent) if check_robots else None
        self.default_interval = 3.0

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch_robots(self, url: str) -> tuple[int, str]:
        await self._limiter.wait(urlsplit(url).netloc, self.default_interval)
        response = await self._client.get(url)
        return response.status_code, response.text

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableError) and exc.retry_after is not None:
            return min(exc.retry_after, self._max_backoff)
        return float(min(self._backoff_base * 2 ** (state.attempt_number - 1), self._max_backoff))

    async def fetch(
        self,
        request: FetchRequest,
        *,
        min_interval: float | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        reject_markers: Sequence[str] = (),
    ) -> FetchedDocument:
        """Fetch with robots check, rate limiting and retries.

        Raises RobotsDisallowed, HttpStatusError (non-retryable 4xx), or RetryableError
        once retries are exhausted. A 304 is returned as a document with `not_modified`.
        """
        if self._robots is not None:
            if not await self._robots.can_fetch(request.url):
                raise RobotsDisallowed(request.url)
            crawl_delay = await self._robots.crawl_delay(request.url)
        else:
            crawl_delay = None
        interval = max(min_interval or self.default_interval, crawl_delay or 0.0)

        headers: dict[str, str] = {}
        if request.method == "GET":
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            retry=retry_if_exception_type(RetryableError),
            wait=self._wait,
            sleep=self._sleep,
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._attempt(request, headers, interval, reject_markers)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _attempt(
        self,
        request: FetchRequest,
        headers: dict[str, str],
        interval: float,
        reject_markers: Sequence[str],
    ) -> FetchedDocument:
        host = urlsplit(request.url).netloc
        await self._limiter.wait(host, interval)
        try:
            response = await self._client.request(
                request.method,
                request.url,
                params=dict(request.params) or None,
                json=request.json_body,
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise RetryableError(request.url, f"network error: {exc!r}") from exc

        if response.status_code in RETRYABLE_STATUS:
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            if response.status_code == 429:
                self._limiter.defer(host, retry_after or self._backoff_base)
            raise RetryableError(request.url, f"HTTP {response.status_code}", retry_after)
        if response.status_code >= 400:
            raise HttpStatusError(request.url, response.status_code)

        content = response.content
        # Some sites serve a block/"try again later" page with a 200 status. Never let
        # that become a snapshot.
        for marker in reject_markers:
            if marker.encode() in content:
                self._limiter.defer(host, self._backoff_base)
                raise RetryableError(request.url, f"block page detected ({marker!r})")

        return FetchedDocument(
            url=str(response.url),
            status_code=response.status_code,
            content=content,
            content_type=response.headers.get("Content-Type"),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            fetched_at=datetime.now(UTC),
        )
