"""Country names and codes to ISO 3166-1 alpha-2 (SPEC §7)."""

import pycountry

from fundscout.normalise.text import normalise_key

# Common names pycountry does not resolve (keys are normalise_key() forms).
_ALIASES = {
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "usa": "US",
    "us": "US",
    "u s": "US",
    "u s a": "US",
    "united states of america": "US",
    "drc": "CD",
    "dr congo": "CD",
    "democratic republic of congo": "CD",
    "democratic republic of the congo": "CD",
    "congo kinshasa": "CD",
    "congo brazzaville": "CG",
    "republic of congo": "CG",
    "republic of the congo": "CG",
    "ivory coast": "CI",
    "cote d ivoire": "CI",
    "cape verde": "CV",
    "swaziland": "SZ",
    "the gambia": "GM",
    "tanzania": "TZ",
    "russia": "RU",
    "south korea": "KR",
    "north korea": "KP",
    "vietnam": "VN",
    "laos": "LA",
    "iran": "IR",
    "syria": "SY",
    "bolivia": "BO",
    "venezuela": "VE",
    "moldova": "MD",
    "palestine": "PS",
    "czech republic": "CZ",
    "turkey": "TR",
    "turkiye": "TR",
    "burma": "MM",
    "kosovo": "XK",
}


def to_alpha2(value: str | None) -> str | None:
    """Return the alpha-2 code for a country name or code, or None if unknown."""
    if not value or not value.strip():
        return None
    text = value.strip()
    key = normalise_key(text)
    if key in _ALIASES:
        return _ALIASES[key]
    try:
        country = pycountry.countries.lookup(text)
    except LookupError:
        return None
    return str(country.alpha_2)
