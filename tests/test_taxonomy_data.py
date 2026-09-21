"""Validation of the taxonomy seed files. No database needed."""

from pathlib import Path

import pytest

from fundscout.taxonomy.loader import (
    DATA_DIR,
    TAXONOMIES,
    TaxonomyError,
    load_taxonomy,
    region_countries,
)

# Starting sets from SPEC §7, verbatim.
SPEC_THEMES = [
    "education",
    "health",
    "climate-environment",
    "agriculture-food-security",
    "water-sanitation",
    "gender-equality",
    "youth",
    "children",
    "human-rights-governance",
    "democracy-civic-space",
    "economic-development-livelihoods",
    "entrepreneurship",
    "technology-innovation",
    "arts-culture",
    "research",
    "humanitarian-emergency",
    "peace-security",
    "disability-inclusion",
    "energy",
    "media-journalism",
]
SPEC_ORG_TYPES = [
    "nonprofit-ngo",
    "community-based-org",
    "social-enterprise",
    "sme-for-profit",
    "startup",
    "university-research-institution",
    "government-public-body",
    "faith-based-org",
    "network-coalition",
    "individual",
]
SPEC_GRANT_TYPES = [
    "project",
    "core-operational",
    "capacity-building",
    "research",
    "fellowship",
    "prize-award",
    "emergency-response",
    "other",
]
SPEC_AFRICA_SUBREGIONS = {
    "west-africa",
    "east-africa",
    "central-africa",
    "southern-africa",
    "north-africa",
}


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("themes", SPEC_THEMES), ("org_types", SPEC_ORG_TYPES), ("grant_types", SPEC_GRANT_TYPES)],
)
def test_seed_slugs_match_spec(kind: str, expected: list[str]) -> None:
    assert [e.slug for e in load_taxonomy(kind)] == expected


def test_all_seed_files_valid() -> None:
    for kind in TAXONOMIES:
        assert load_taxonomy(kind)


def test_regions_structure() -> None:
    entries = load_taxonomy("regions")
    by_slug = {e.slug: e for e in entries}
    assert {"africa", "americas", "asia", "europe", "oceania", "global"} <= by_slug.keys()
    assert by_slug.keys() >= SPEC_AFRICA_SUBREGIONS
    assert by_slug["west-africa"].parent == "sub-saharan-africa"
    assert by_slug["north-africa"].parent == "africa"


def test_every_country_in_exactly_one_leaf_region() -> None:
    entries = load_taxonomy("regions")
    geo = [e for e in entries if e.slug != "global-south"]
    seen: dict[str, str] = {}
    for e in geo:
        for code in e.countries:
            assert code not in seen, f"{code} in both {seen[code]} and {e.slug}"
            seen[code] = e.slug
    assert seen["TW"] == "east-asia"
    assert seen["NO"] == "northern-europe"
    assert "AQ" not in seen


def test_region_country_union() -> None:
    countries = region_countries(load_taxonomy("regions"))
    assert {"NG", "GH", "SN"} <= countries["west-africa"]
    assert countries["west-africa"] <= countries["sub-saharan-africa"] <= countries["africa"]
    assert "EG" in countries["africa"] and "EG" not in countries["sub-saharan-africa"]
    assert countries["global"] == set()
    assert {"NG", "IN", "BR"} <= countries["global-south"]
    assert not {"GB", "US", "JP", "AU"} & countries["global-south"]


def _write(tmp_path: Path, kind: str, body: str) -> Path:
    for other in TAXONOMIES:
        (tmp_path / f"{other}.yaml").write_text((DATA_DIR / f"{other}.yaml").read_text())
    (tmp_path / f"{kind}.yaml").write_text(body)
    return tmp_path


@pytest.mark.parametrize(
    ("kind", "body", "message"),
    [
        ("themes", "- {slug: a, label: A}\n- {slug: a, label: B}\n", "duplicate slug"),
        ("themes", "- {slug: a, label: A, parent: zz}\n", "unknown parent"),
        (
            "themes",
            "- {slug: a, label: A, parent: b}\n- {slug: b, label: B, parent: a}\n",
            "cycle",
        ),
        ("themes", "- {slug: Bad Slug, label: A}\n", "slug"),
        ("themes", "- {slug: a, label: A, countries: ['GH']}\n", "only regions"),
        ("regions", "- {slug: a, label: A, countries: ['XX']}\n", "not an ISO"),
        ("regions", "- {slug: a, label: A, countries: [NO]}\n", "valid string"),
        ("regions", "- {slug: a, label: A, countries: ['GH', 'GH']}\n", "more than once"),
        ("themes", "- {slug: a, label: A, colour: red}\n", "Extra inputs"),
    ],
)
def test_invalid_taxonomy_rejected(tmp_path: Path, kind: str, body: str, message: str) -> None:
    with pytest.raises(TaxonomyError, match=message):
        load_taxonomy(kind, _write(tmp_path, kind, body))
