"""Taxonomy lookups for extraction prompts and normalisation, built from the YAML seeds
(no database needed)."""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from fundscout.normalise.text import normalise_key
from fundscout.taxonomy.loader import DATA_DIR, TAXONOMIES, load_taxonomy, region_countries


@dataclass
class TaxonomyIndex:
    slugs: dict[str, list[str]] = field(default_factory=dict)  # kind -> slugs (YAML order)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)  # kind -> slug -> label
    _lookup: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # kind -> key -> slugs
    region_countries: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, data_dir: Path = DATA_DIR) -> "TaxonomyIndex":
        index = cls()
        for kind in TAXONOMIES:
            entries = load_taxonomy(kind, data_dir)
            index.slugs[kind] = [e.slug for e in entries]
            index.labels[kind] = {e.slug: e.label for e in entries}
            lookup: dict[str, list[str]] = {}
            for e in entries:
                for name in (e.slug, e.label, *e.aliases):
                    slugs = lookup.setdefault(normalise_key(name), [])
                    if e.slug not in slugs:
                        slugs.append(e.slug)
            index._lookup[kind] = lookup
            if kind == "regions":
                index.region_countries = region_countries(entries)
        return index

    def resolve(self, kind: str, value: str) -> list[str]:
        """Slugs for a slug, label or alias (case-insensitive); [] if unknown."""
        return list(self._lookup[kind].get(normalise_key(value), []))


@lru_cache
def default_taxonomy() -> TaxonomyIndex:
    return TaxonomyIndex.from_yaml()
