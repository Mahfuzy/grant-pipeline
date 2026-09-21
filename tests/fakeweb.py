"""In-memory web for tests: serves saved fixtures through httpx.MockTransport."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures"

Handler = Callable[[httpx.Request], httpx.Response]


def fixture_bytes(relative: str) -> bytes:
    return (FIXTURES / relative).read_bytes()


@dataclass
class FakeWeb:
    """Routes are matched on (method, full URL) first, then (method, URL without query),
    then prefix routes. Unmatched requests get a 404."""

    routes: dict[tuple[str, str], Handler] = field(default_factory=dict)
    prefix_routes: list[tuple[str, str, Handler]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(
        self,
        url: str,
        body: bytes | str = b"",
        *,
        method: str = "GET",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        content = body.encode() if isinstance(body, str) else body
        self.routes[(method, url)] = lambda _req: httpx.Response(
            status, content=content, headers=headers or {}
        )

    def handle(self, url: str, handler: Handler, *, method: str = "GET") -> None:
        self.routes[(method, url)] = handler

    def handle_prefix(self, prefix: str, handler: Handler, *, method: str = "GET") -> None:
        self.prefix_routes.append((method, prefix, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        base = url.split("?", 1)[0]
        handler = self.routes.get((request.method, url)) or self.routes.get((request.method, base))
        if handler is None:
            for method, prefix, h in self.prefix_routes:
                if method == request.method and url.startswith(prefix):
                    handler = h
                    break
        if handler is None:
            return httpx.Response(404, content=b"not found")
        return handler(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def requests_to(self, prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).startswith(prefix)]


def json_body(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


async def no_sleep(_seconds: float) -> None:
    return None
