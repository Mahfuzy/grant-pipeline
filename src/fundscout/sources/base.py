"""Source adapter interface (SPEC §10).

An adapter turns a source's config into discovered items and fetched documents. The
pipeline runner handles everything around it: run bookkeeping, change detection,
raw storage and error isolation.
"""

import hashlib
import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urldefrag

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fundscout.config import Settings
from fundscout.db.models import RawDocument, Source
from fundscout.extract.pipeline import ExtractionConfig
from fundscout.fetch.hashing import HashSpec, content_hash
from fundscout.fetch.http import FetchedDocument, FetchRequest, HttpClient


class UrlRewrite(BaseModel):
    """Map a discovered link to a canonical identity URL and/or a different fetch URL.

    `match` is a regex with named groups, matched against the whole link (query string
    included). `url` and `fetch_url` are `str.format` templates over those groups; each
    group `name` is also available lowercased as `name_lower`.
    """

    model_config = ConfigDict(extra="forbid")

    match: str
    url: str | None = None
    fetch_url: str | None = None

    @field_validator("match")
    @classmethod
    def _valid_regex(cls, value: str) -> str:
        re.compile(value)
        return value


class AdapterConfig(BaseModel):
    """Options shared by all adapters. Adapters extend this with their own fields."""

    model_config = ConfigDict(extra="forbid")

    rate_limit_seconds: float | None = Field(default=None, gt=0)
    content_hash: HashSpec = HashSpec()
    # Substrings that mark a block/"try again later" page served with a 2xx status.
    reject_markers: list[str] = []
    # If an item's discovery data is unchanged since its last snapshot, skip fetching it
    # until the snapshot is this many days old. None: always fetch.
    refetch_unchanged_after_days: float | None = Field(default=None, gt=0)
    url_rewrites: list[UrlRewrite] = []
    # The source is the funder's official channel (SPEC §8.2: its values win conflicts
    # and missing from its listing can flag possibly_removed).
    primary_source: bool = False
    # How fetched documents are turned into grants (M3). Default: LLM extraction.
    extraction: ExtractionConfig = ExtractionConfig()


@dataclass(frozen=True)
class DiscoveredItem:
    url: str
    """Canonical identity URL: stored as raw_documents.url and later grant_sources.url."""
    request: FetchRequest
    """How to fetch the document (may differ from `url`, e.g. an API endpoint)."""
    metadata: dict[str, Any] = field(default_factory=dict)
    """What discovery saw (listing fields, feed entry, API search hit)."""
    change_key: str | None = None
    """Hash of the discovery data; lets unchanged items skip fetching (see AdapterConfig)."""
    inline_content: bytes | None = None
    """Set when discovery already returned the full document (no fetch needed)."""


@dataclass
class RunContext:
    source: Source
    run_id: uuid.UUID
    http: HttpClient
    settings: Settings
    limit: int | None = None
    log: structlog.stdlib.BoundLogger = field(default_factory=structlog.get_logger)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class SourceAdapter[ConfigT: AdapterConfig](ABC):
    name: ClassVar[str]
    config_model: ClassVar[type[AdapterConfig]]

    def __init__(self, config: ConfigT):
        self.config = config

    @classmethod
    def validate_config(cls, raw: dict[str, Any]) -> AdapterConfig:
        return cls.config_model.model_validate(raw)

    @abstractmethod
    async def discover(self, source: Source, ctx: RunContext) -> list[DiscoveredItem]: ...

    async def fetch(
        self, item: DiscoveredItem, ctx: RunContext, previous: RawDocument | None = None
    ) -> FetchedDocument:
        """Fetch an item's document, conditionally when a previous snapshot exists."""
        if item.inline_content is not None:
            return FetchedDocument(
                url=item.url,
                status_code=200,
                content=item.inline_content,
                content_type="application/json",
                etag=None,
                last_modified=None,
                fetched_at=datetime.now(UTC),
            )
        return await ctx.http.fetch(
            item.request,
            min_interval=self.rate_limit(ctx),
            etag=previous.etag if previous else None,
            last_modified=previous.last_modified if previous else None,
            reject_markers=self.config.reject_markers,
        )

    def content_hash(self, document: FetchedDocument) -> str:
        return content_hash(document.content, self.config.content_hash)

    def extract(self, raw: RawDocument, ctx: RunContext) -> Any:
        raise NotImplementedError("Extraction is implemented in Milestone 3")

    # Helpers for subclasses ------------------------------------------------------------

    def rate_limit(self, ctx: RunContext) -> float:
        return self.config.rate_limit_seconds or ctx.settings.default_rate_limit_seconds

    async def get(self, ctx: RunContext, request: FetchRequest) -> FetchedDocument:
        """Fetch a discovery page (listing, feed, search results)."""
        return await ctx.http.fetch(
            request, min_interval=self.rate_limit(ctx), reject_markers=self.config.reject_markers
        )

    def resolve_link(self, link: str) -> tuple[str, FetchRequest]:
        """Apply URL rewrites: returns (identity url, fetch request)."""
        link = urldefrag(link).url
        for rule in self.config.url_rewrites:
            m = re.fullmatch(rule.match, link)
            if m is None:
                continue
            groups = {k: v for k, v in m.groupdict().items() if v is not None}
            groups |= {f"{k}_lower": v.lower() for k, v in list(groups.items())}
            identity = rule.url.format(**groups) if rule.url else link
            fetch_url = rule.fetch_url.format(**groups) if rule.fetch_url else identity
            return identity, FetchRequest(fetch_url)
        return link, FetchRequest(link)


def merge_by_url(entries: list[tuple[str, FetchRequest, dict[str, Any]]]) -> list[DiscoveredItem]:
    """Group discovery entries that point at the same document, keeping first-seen order.

    Several listing entries can share one document (e.g. one PDF describing four calls);
    the document is fetched once and all entries are kept in `metadata["entries"]`.
    """
    grouped: dict[str, tuple[FetchRequest, list[dict[str, Any]]]] = {}
    for url, request, entry in entries:
        grouped.setdefault(url, (request, []))[1].append(entry)
    return [
        DiscoveredItem(
            url=url,
            request=request,
            metadata={"entries": group},
            change_key=stable_hash(group),
        )
        for url, (request, group) in grouped.items()
    ]
