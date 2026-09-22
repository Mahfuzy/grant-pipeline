"""Config-driven RSS/Atom adapter: each entry's link is fetched as a document."""

from typing import Any

import feedparser

from fundscout.db.models import Source
from fundscout.fetch.http import FetchRequest
from fundscout.sources.base import (
    AdapterConfig,
    DiscoveredItem,
    RunContext,
    SourceAdapter,
    merge_by_url,
)


class RssConfig(AdapterConfig):
    feed_url: str


def _entry_metadata(entry: Any) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "link": entry.get("link"),
        "title": entry.get("title"),
        "summary": entry.get("summary"),
        "published": entry.get("published"),
        # dict.get bypasses feedparser's deprecated updated->published fallback.
        "updated": dict.get(entry, "updated"),
        "categories": [
            {"term": t.get("term"), "scheme": t.get("scheme")} for t in entry.get("tags", [])
        ],
    }


class GenericRssAdapter(SourceAdapter[RssConfig]):
    name = "generic_rss"
    config_model = RssConfig

    async def discover(self, source: Source, ctx: RunContext) -> list[DiscoveredItem]:
        doc = await self.get(ctx, FetchRequest(self.config.feed_url))
        feed = feedparser.parse(doc.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"could not parse feed: {feed.bozo_exception!r}")
        entries: list[tuple[str, FetchRequest, dict[str, Any]]] = []
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            url, request = self.resolve_link(link)
            entries.append((url, request, _entry_metadata(entry)))
        items = merge_by_url(entries)
        return items[: ctx.limit] if ctx.limit is not None else items
