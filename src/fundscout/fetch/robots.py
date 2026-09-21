"""robots.txt handling per RFC 9309.

- 2xx: parse and apply the rules.
- 4xx (including 401/403): no restrictions ("unavailable").
- 5xx or network failure: disallow everything ("unreachable").

Matching: the group whose user-agent token matches ours (case-insensitive substring of
our product token) is used, else the `*` group. Within a group the longest matching
rule wins; on a tie, Allow wins. `*` matches any sequence and a trailing `$` anchors.
"""

import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(body + ("$" if anchored else ""))


class RobotsRules:
    def __init__(self, groups: list[_Group], allow_all: bool = False, deny_all: bool = False):
        self._groups = groups
        self.allow_all = allow_all
        self.deny_all = deny_all

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue
            if key in ("allow", "disallow"):
                if value:  # an empty Disallow means "allow everything"
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    current.crawl_delay = float(value)
        return cls(groups)

    def _group_for(self, user_agent: str) -> _Group | None:
        token = user_agent.lower()
        specific = [g for g in self._groups if any(a != "*" and a in token for a in g.agents)]
        if specific:
            return specific[0]
        return next((g for g in self._groups if "*" in g.agents), None)

    def can_fetch(self, url: str, user_agent: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        group = self._group_for(user_agent)
        if group is None:
            return True
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern in group.rules:
            if _pattern_regex(pattern).match(path):
                candidate = (len(pattern), allow)
                if best is None or candidate > best:
                    best = candidate
        return True if best is None else best[1]

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self._group_for(user_agent)
        return group.crawl_delay if group else None


# Returns (status_code, body) for a robots.txt URL, or raises on network failure.
RobotsFetcher = Callable[[str], Awaitable[tuple[int, str]]]


class RobotsChecker:
    """Fetches and caches robots.txt per origin."""

    def __init__(self, fetcher: RobotsFetcher, user_agent: str):
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._cache: dict[str, RobotsRules] = {}

    async def rules_for(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = await self._load(origin + "/robots.txt")
        return self._cache[origin]

    async def _load(self, robots_url: str) -> RobotsRules:
        try:
            status, body = await self._fetcher(robots_url)
        except Exception:
            return RobotsRules([], deny_all=True)
        if 200 <= status < 300:
            return RobotsRules.parse(body)
        if 400 <= status < 500:
            return RobotsRules([], allow_all=True)
        return RobotsRules([], deny_all=True)

    async def can_fetch(self, url: str) -> bool:
        return (await self.rules_for(url)).can_fetch(url, self._user_agent)

    async def crawl_delay(self, url: str) -> float | None:
        return (await self.rules_for(url)).crawl_delay(self._user_agent)
