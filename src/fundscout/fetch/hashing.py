"""Content hashing for change detection.

Raw bytes are always stored verbatim, but the hash used to decide "unchanged" can be
computed over a canonical form, because some responses are never byte-identical:
e.g. Grants.gov adds a fresh `token` to every response and returns some lists in a
random order.
"""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from selectolax.parser import HTMLParser


class HashSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["raw", "json", "html"] = "raw"
    # json: keys to drop anywhere in the document; sort lists so order changes are ignored.
    exclude_keys: list[str] = []
    sort_lists: bool = False
    # html: hash only this element (default: whole document), minus these elements.
    selector: str | None = None
    exclude_selectors: list[str] = []


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any, spec: HashSpec) -> Any:
    if isinstance(value, dict):
        return {k: _canonical_json(v, spec) for k, v in value.items() if k not in spec.exclude_keys}
    if isinstance(value, list):
        items = [_canonical_json(v, spec) for v in value]
        if spec.sort_lists:
            items.sort(key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))
        return items
    return value


def content_hash(content: bytes, spec: HashSpec) -> str:
    if spec.mode == "json":
        try:
            data = json.loads(content)
        except ValueError:
            return sha256_hex(content)
        canonical = json.dumps(
            _canonical_json(data, spec), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return sha256_hex(canonical.encode())
    if spec.mode == "html":
        tree = HTMLParser(content)
        node = tree.css_first(spec.selector) if spec.selector else tree.root
        if node is None:
            return sha256_hex(content)
        for selector in [*spec.exclude_selectors, "script", "style"]:
            for el in node.css(selector):
                el.decompose()
        return sha256_hex((node.html or "").encode())
    return sha256_hex(content)
