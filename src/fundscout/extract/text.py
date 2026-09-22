"""Turn stored documents into text for extraction, and trim very long text (SPEC §6.2)."""

import json
import re
from dataclasses import dataclass

import trafilatura
from selectolax.parser import HTMLParser

from fundscout.extract.pdf import pdf_to_text
from fundscout.normalise.text import clean_text, html_to_text

# Paragraphs mentioning these are kept when a page has to be shortened.
_IMPORTANT = re.compile(
    r"deadline|closing|close[sd]?\b|due date|submission|eligib|who can apply|applicant|"
    r"amount|budget|funding|award|grant size|up to|maximum|minimum|€|\$|£|₦|₵|"
    r"\b(USD|EUR|GBP)\b|apply|application|open(s|ing)?\b|date",
    re.IGNORECASE,
)


def kind_of(content_type: str | None, content: bytes) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct or content[:5] == b"%PDF-":
        return "pdf"
    if "json" in ct:
        return "json"
    if "html" in ct or b"<html" in content[:2000].lower():
        return "html"
    return "text"


def html_main_text(html: str, content_selector: str = "main") -> str:
    """Page title and headline plus the main content element. Falls back to trafilatura's
    main-content detection when the page has no such element."""
    tree = HTMLParser(html)
    parts: list[str] = []
    for selector in ("title", "h1"):
        node = tree.css_first(selector)
        text = clean_text(node.text()) if node else None
        if text and text not in parts:
            parts.append(text)
    main = tree.css_first(content_selector)
    if main is not None:
        body = html_to_text(main.html)
    else:
        body = clean_text(
            trafilatura.extract(html, include_tables=True, include_links=True, favor_recall=True)
        )
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def document_text(content: bytes, content_type: str | None, content_selector: str = "main") -> str:
    kind = kind_of(content_type, content)
    if kind == "pdf":
        return pdf_to_text(content)
    decoded = content.decode("utf-8", errors="replace")
    if kind == "html":
        return html_main_text(decoded, content_selector)
    if kind == "json":
        return json.dumps(json.loads(decoded), indent=1, ensure_ascii=False)
    return clean_text(decoded) or ""


@dataclass(frozen=True)
class PreparedText:
    text: str
    truncated: bool
    original_chars: int


def prepare_text(text: str, max_chars: int) -> PreparedText:
    """Keep text under max_chars. When shortening, keep the opening (title, summary) and
    then paragraphs that mention deadlines, eligibility or amounts, in document order."""
    if len(text) <= max_chars:
        return PreparedText(text, False, len(text))
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    head_budget = max_chars // 4
    keep: list[bool] = [False] * len(paragraphs)
    used = 0
    for i, p in enumerate(paragraphs):
        if used + len(p) > head_budget:
            break
        keep[i] = True
        used += len(p) + 2
    for i, p in enumerate(paragraphs):
        if keep[i] or not _IMPORTANT.search(p):
            continue
        if used + len(p) + 2 > max_chars:
            continue
        keep[i] = True
        used += len(p) + 2
    kept = [p if keep[i] else None for i, p in enumerate(paragraphs)]
    parts: list[str] = []
    for p in kept:
        if p is not None:
            parts.append(p)
        elif parts and parts[-1] != "[…]":
            parts.append("[…]")
    return PreparedText("\n\n".join(parts), True, len(text))
