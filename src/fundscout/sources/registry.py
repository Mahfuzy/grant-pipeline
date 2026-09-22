"""Maps adapter names (sources.adapter) to adapter classes."""

from typing import Any

from fundscout.sources.adapters.generic_api import GenericApiAdapter
from fundscout.sources.adapters.generic_html_listing import GenericHtmlListingAdapter
from fundscout.sources.adapters.generic_rss import GenericRssAdapter
from fundscout.sources.base import SourceAdapter

ADAPTERS: dict[str, type[SourceAdapter[Any]]] = {
    cls.name: cls for cls in (GenericApiAdapter, GenericHtmlListingAdapter, GenericRssAdapter)
}


class UnknownAdapter(KeyError):
    pass


def get_adapter_class(name: str) -> type[SourceAdapter[Any]]:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise UnknownAdapter(f"unknown adapter {name!r}; known: {sorted(ADAPTERS)}") from None


def build_adapter(name: str, config: dict[str, Any]) -> SourceAdapter[Any]:
    cls = get_adapter_class(name)
    return cls(cls.validate_config(config))
