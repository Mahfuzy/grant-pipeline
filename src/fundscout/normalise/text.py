"""Text cleanup helpers."""

import re
import unicodedata

from selectolax.parser import HTMLParser

_WS = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "dt", "dd")


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace; empty strings become None."""
    if value is None:
        return None
    text = _WS.sub(" ", value)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_LINES.sub("\n\n", text).strip()
    return text or None


def html_to_text(html: str | None) -> str | None:
    """Plain text from an HTML fragment, keeping paragraph breaks."""
    if not html:
        return None
    tree = HTMLParser(html)
    for node in tree.css("script, style"):
        node.decompose()
    for tag in _BLOCK_TAGS:
        for node in tree.css(tag):
            node.insert_after("\n")
    root = tree.body or tree.root
    return clean_text(root.text(separator="") if root else None)


def truncate(value: str | None, max_chars: int) -> str | None:
    """Shorten to at most max_chars, preferring a sentence or word boundary."""
    if value is None or len(value) <= max_chars:
        return value
    cut = value[: max_chars - 1]
    boundary = max(cut.rfind(". "), cut.rfind("\n"))
    if boundary > max_chars * 0.5:
        return cut[: boundary + 1].rstrip()
    return cut.rsplit(" ", 1)[0].rstrip() + "…"


def normalise_key(value: str) -> str:
    """Case- and accent-insensitive key: lowercase ASCII words separated by single spaces."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", ascii_only.casefold()).split())


def normalise_title(title: str) -> str:
    return normalise_key(title)
