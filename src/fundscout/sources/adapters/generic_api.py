"""Config-driven JSON API adapter.

Discovery pages through a search endpoint; each hit becomes an item. If a `detail`
request is configured it is fetched per hit, otherwise the hit itself is the document.

Templating: in `item_url` and in `detail` URL/params/body, `{field}` is replaced by the
hit's value. A body/param value that is exactly `"{field}"` keeps the value's JSON type.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fundscout.db.models import Source
from fundscout.fetch.http import FetchRequest
from fundscout.sources.base import (
    AdapterConfig,
    DiscoveredItem,
    RunContext,
    SourceAdapter,
    stable_hash,
)


class ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, Any] = {}
    body: Any = None


class OffsetPagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_param: str
    page_size: int = Field(ge=1)
    size_param: str | None = None
    location: Literal["body", "query"] = "query"
    start: int = 0
    max_pages: int = Field(default=100, ge=1)


class ApiConfig(AdapterConfig):
    search: ApiRequest
    pagination: OffsetPagination | None = None
    results_path: str  # dotted path to the list of hits, e.g. "data.oppHits"
    total_path: str | None = None  # dotted path to the total hit count
    id_field: str
    item_url: str  # canonical identity URL template, e.g. "https://example.org/opp/{id}"
    # Hit fields whose change triggers a re-fetch (default: the whole hit).
    change_fields: list[str] | None = None
    detail: ApiRequest | None = None


def get_path(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def render(template: Any, values: dict[str, Any]) -> Any:
    if isinstance(template, str):
        if template.startswith("{") and template.endswith("}") and template[1:-1] in values:
            return values[template[1:-1]]
        return template.format_map(values)
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


class GenericApiAdapter(SourceAdapter[ApiConfig]):
    name = "generic_api"
    config_model = ApiConfig

    def _search_request(self, offset: int | None) -> FetchRequest:
        search = self.config.search
        params = {k: str(v) for k, v in search.params.items()}
        body = dict(search.body) if isinstance(search.body, dict) else search.body
        pag = self.config.pagination
        if pag is not None and offset is not None:
            paging: dict[str, Any] = {pag.offset_param: offset}
            if pag.size_param:
                paging[pag.size_param] = pag.page_size
            if pag.location == "body":
                body = {**(body or {}), **paging}
            else:
                params |= {k: str(v) for k, v in paging.items()}
        return FetchRequest(search.url, method=search.method, json_body=body, params=params)

    async def discover(self, source: Source, ctx: RunContext) -> list[DiscoveredItem]:
        pag = self.config.pagination
        items: dict[str, DiscoveredItem] = {}
        offset = pag.start if pag else None
        for _ in range(pag.max_pages if pag else 1):
            doc = await self.get(ctx, self._search_request(offset))
            data = json.loads(doc.content)
            hits = get_path(data, self.config.results_path) or []
            if not isinstance(hits, list):
                raise ValueError(f"{self.config.results_path} is not a list")
            for hit in hits:
                item = self._item(hit)
                items.setdefault(item.url, item)  # pages can overlap if results shift
                if ctx.limit is not None and len(items) >= ctx.limit:
                    return list(items.values())
            if pag is None or offset is None or not hits:
                break
            offset += pag.page_size
            total = get_path(data, self.config.total_path) if self.config.total_path else None
            if isinstance(total, int) and offset >= total:
                break
        return list(items.values())

    def _item(self, hit: dict[str, Any]) -> DiscoveredItem:
        if self.config.id_field not in hit:
            raise ValueError(f"hit has no {self.config.id_field!r} field")
        url = render(self.config.item_url, hit)
        fields = self.config.change_fields
        change_key = stable_hash({f: hit.get(f) for f in fields} if fields else hit)
        detail = self.config.detail
        if detail is None:
            return DiscoveredItem(
                url=url,
                request=FetchRequest(url),
                metadata={"hit": hit},
                change_key=change_key,
                inline_content=json.dumps(hit, ensure_ascii=False).encode(),
            )
        request = FetchRequest(
            render(detail.url, hit),
            method=detail.method,
            json_body=render(detail.body, hit),
            params={k: str(render(v, hit)) for k, v in detail.params.items()},
        )
        return DiscoveredItem(
            url=url, request=request, metadata={"hit": hit}, change_key=change_key
        )
