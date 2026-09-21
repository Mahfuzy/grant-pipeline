"""Load taxonomy seed data from YAML and sync it into the taxonomy tables.

The YAML files in `taxonomy/data/` are the source of truth (SPEC §7). Seeding is
idempotent: rows are matched on `slug`, labels/parents/country lists are updated in
place, and rows that are no longer in YAML are reported but never deleted (grants may
still reference them).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pycountry
import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.db.models import GrantType, OrgType, Region, RegionCountry, TaxonomyBase, Theme

DATA_DIR = Path(__file__).parent / "data"

TAXONOMIES: Mapping[str, type[TaxonomyBase]] = {
    "themes": Theme,
    "regions": Region,
    "org_types": OrgType,
    "grant_types": GrantType,
}


class TaxonomyError(ValueError):
    pass


class TaxonomyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=100)
    label: str = Field(min_length=1, max_length=200)
    parent: str | None = None
    countries: tuple[str, ...] = ()


_entries_adapter = TypeAdapter(list[TaxonomyEntry])


def load_taxonomy(kind: str, data_dir: Path = DATA_DIR) -> list[TaxonomyEntry]:
    """Parse and validate one taxonomy YAML file."""
    if kind not in TAXONOMIES:
        raise TaxonomyError(f"unknown taxonomy {kind!r}")
    path = data_dir / f"{kind}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    try:
        entries = _entries_adapter.validate_python(raw)
    except ValueError as exc:
        raise TaxonomyError(f"{path}: {exc}") from exc
    _validate(kind, entries, path)
    return entries


def _validate(kind: str, entries: list[TaxonomyEntry], path: Path) -> None:
    slugs: set[str] = set()
    for e in entries:
        if e.slug in slugs:
            raise TaxonomyError(f"{path}: duplicate slug {e.slug!r}")
        slugs.add(e.slug)

    by_slug = {e.slug: e for e in entries}
    for e in entries:
        if e.parent is not None and e.parent not in by_slug:
            raise TaxonomyError(f"{path}: {e.slug!r} has unknown parent {e.parent!r}")
        # Walk up the parent chain to detect cycles.
        seen = {e.slug}
        node = e
        while node.parent is not None:
            if node.parent in seen:
                raise TaxonomyError(f"{path}: parent cycle involving {e.slug!r}")
            seen.add(node.parent)
            node = by_slug[node.parent]

        if e.countries and kind != "regions":
            raise TaxonomyError(f"{path}: {e.slug!r}: only regions can list countries")
        if len(set(e.countries)) != len(e.countries):
            raise TaxonomyError(f"{path}: {e.slug!r} lists a country more than once")
        for code in e.countries:
            if pycountry.countries.get(alpha_2=code) is None:
                raise TaxonomyError(
                    f"{path}: {e.slug!r}: {code!r} is not an ISO 3166-1 alpha-2 code"
                )


def region_countries(entries: list[TaxonomyEntry]) -> dict[str, set[str]]:
    """Countries covered by each region: its own plus those of all descendants."""
    children: dict[str, list[str]] = {e.slug: [] for e in entries}
    for e in entries:
        if e.parent is not None:
            children[e.parent].append(e.slug)
    own = {e.slug: set(e.countries) for e in entries}

    result: dict[str, set[str]] = {}

    def collect(slug: str) -> set[str]:
        if slug not in result:
            result[slug] = own[slug].union(*(collect(c) for c in children[slug]))
        return result[slug]

    for e in entries:
        collect(e.slug)
    return result


@dataclass
class SeedResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    not_in_yaml: list[str] = field(default_factory=list)


def seed_taxonomy(session: Session, data_dir: Path = DATA_DIR) -> dict[str, SeedResult]:
    """Sync all taxonomy tables with the YAML files. Flushes but does not commit."""
    # Validate everything before touching the database.
    loaded = {kind: load_taxonomy(kind, data_dir) for kind in TAXONOMIES}
    return {kind: _seed_one(session, TAXONOMIES[kind], loaded[kind]) for kind in TAXONOMIES}


def _seed_one(
    session: Session, model: type[TaxonomyBase], entries: list[TaxonomyEntry]
) -> SeedResult:
    result = SeedResult()
    rows = {row.slug: row for row in session.scalars(select(model))}
    changed: set[str] = set()
    created: set[str] = set()

    for e in entries:
        row = rows.get(e.slug)
        if row is None:
            row = model(slug=e.slug, label=e.label)
            session.add(row)
            rows[e.slug] = row
            created.add(e.slug)
        elif row.label != e.label:
            row.label = e.label
            changed.add(e.slug)
    session.flush()  # assign ids before resolving parents

    for e in entries:
        row = rows[e.slug]
        parent_id = rows[e.parent].id if e.parent is not None else None
        if row.parent_id != parent_id:
            row.parent_id = parent_id
            changed.add(e.slug)

    if model is Region:
        for slug, codes in region_countries(entries).items():
            region = rows[slug]
            assert isinstance(region, Region)
            current = {rc.country_code: rc for rc in region.countries}
            for code in codes - current.keys():
                region.countries.append(RegionCountry(country_code=code))
            for code in current.keys() - codes:
                region.countries.remove(current[code])
            if codes != current.keys():
                changed.add(slug)
    session.flush()

    yaml_slugs = {e.slug for e in entries}
    result.created = len(created)
    result.updated = len(changed - created)
    result.unchanged = len(yaml_slugs) - result.created - result.updated
    result.not_in_yaml = sorted(set(rows) - yaml_slugs)
    return result
