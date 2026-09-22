"""Funder name normalisation (SPEC §7)."""

import re

from fundscout.normalise.text import normalise_key

# Legal-form and generic suffixes dropped from normalised_name (kept in the display name).
_SUFFIXES = {
    "foundation",
    "fund",
    "trust",
    "inc",
    "incorporated",
    "ltd",
    "limited",
    "llc",
    "plc",
    "gmbh",
    "co",
    "corp",
    "corporation",
}


def normalise_funder_name(name: str) -> str:
    """Lowercased, punctuation stripped, leading "the", parenthesised parts (usually an
    acronym, "... Health (NIH)") and trailing suffixes removed."""
    words = normalise_key(re.sub(r"\([^)]*\)", " ", name)).split() or normalise_key(name).split()
    if words[:1] == ["the"]:
        words = words[1:]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)
