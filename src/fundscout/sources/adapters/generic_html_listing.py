"""Config-driven HTML listing adapter: listing page(s) -> detail links."""

from typing import Any, Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser, Node

from fundscout.db.models import Source
from fundscout.fetch.http import FetchRequest
from fundscout.sources.base import (
    AdapterConfig,
    DiscoveredItem,
    RunContext,
    SourceAdapter,
    merge_by_url,
)


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["next_link"] = "next_link"
    selector: str  # CSS selector for the "next page" link
    max_pages: int = Field(default=20, ge=1)


class HtmlListingConfig(AdapterConfig):
    listing_url: str
    item_selector: str  # one element per listed opportunity
    link_selector: str  # detail link, relative to the item
    # Extra fields to capture per item: name -> CSS selector (text content, relative to item).
    # Multiple matches are joined with " | ".
    item_fields: dict[str, str] = Field(default_factory=dict)
    # Optional selector for a <dl> inside each item; its <dt>/<dd> pairs are captured as
    # {"term": ["value", ...]} in the entry's "definitions".
    definition_list: str | None = None
    pagination: Pagination | None = None


def _definitions(dl: Node) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    term: str | None = None
    for child in dl.iter():
        if child.tag == "dt":
            term = child.text(strip=True)
            result.setdefault(term, [])
        elif child.tag == "dd" and term is not None:
            values = [li.text(strip=True) for li in child.css("li")] or [child.text(strip=True)]
            result[term].extend(v for v in values if v)
    return result


def _text(node: Node, selector: str) -> str | None:
    matches = [m.text(strip=True) for m in node.css(selector)]
    matches = [m for m in matches if m]
    return " | ".join(matches) if matches else None


class GenericHtmlListingAdapter(SourceAdapter[HtmlListingConfig]):
    name = "generic_html_listing"
    config_model = HtmlListingConfig

    async def discover(self, source: Source, ctx: RunContext) -> list[DiscoveredItem]:
        entries: list[tuple[str, FetchRequest, dict[str, Any]]] = []
        page_url: str | None = self.config.listing_url
        max_pages = self.config.pagination.max_pages if self.config.pagination else 1
        seen_pages: set[str] = set()

        for _ in range(max_pages):
            if page_url is None or page_url in seen_pages:
                break
            seen_pages.add(page_url)
            doc = await self.get(ctx, FetchRequest(page_url))
            tree = HTMLParser(doc.content)
            for node in tree.css(self.config.item_selector):
                link = node.css_first(self.config.link_selector)
                href = link.attributes.get("href") if link else None
                if not href:
                    continue
                absolute = urljoin(doc.url, href)
                url, request = self.resolve_link(absolute)
                entry: dict[str, Any] = {"link": absolute, "listing_url": page_url}
                entry |= {name: _text(node, sel) for name, sel in self.config.item_fields.items()}
                if self.config.definition_list:
                    dl = node.css_first(self.config.definition_list)
                    entry["definitions"] = _definitions(dl) if dl else {}
                entries.append((url, request, entry))
            items = merge_by_url(entries)
            if ctx.limit is not None and len(items) >= ctx.limit:
                return items[: ctx.limit]
            page_url = self._next_page(tree, doc.url)
        return merge_by_url(entries)

    def _next_page(self, tree: HTMLParser, current_url: str) -> str | None:
        if self.config.pagination is None:
            return None
        link = tree.css_first(self.config.pagination.selector)
        href = link.attributes.get("href") if link else None
        return urljoin(current_url, href) if href else None
