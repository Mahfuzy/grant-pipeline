"""Find the existing grant an extracted grant belongs to (SPEC §8.1).

1. Exact (source, url, item_key) link in grant_sources.
2. Fingerprint: sha256 of normalised funder + normalised title + closing date.
3. Fuzzy: same funder, title token_set_ratio >= the match threshold, compatible dates.
4. Scores between the possible-duplicate and match thresholds are returned as possible
   duplicates (a new grant is created and a review item links them).

A listing gives distinct opportunities distinct URLs, so grants already linked to a
*different* document of the same source are never merged by steps 2-3, and similar titles
among them (e.g. topics of one call) are not flagged either; only an exact fingerprint
collision within a source is reported as a possible duplicate.
"""

import hashlib
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.db.models import Grant, GrantSource

# At most this many possible-duplicate review items per new grant (best scores first).
MAX_POSSIBLE = 5


def fingerprint(normalised_funder: str | None, normalised_title: str, closing: date | None) -> str:
    key = "|".join(
        (normalised_funder or "", normalised_title, closing.isoformat() if closing else "")
    )
    return hashlib.sha256(key.encode()).hexdigest()


def dates_compatible(a: date | None, b: date | None, tolerance_days: int) -> bool:
    return a is None or b is None or abs((a - b).days) <= tolerance_days


@dataclass
class MatchQuery:
    source_id: uuid.UUID
    url: str
    item_key: str
    funder_id: uuid.UUID | None
    normalised_title: str
    closing_date: date | None
    fingerprint: str
    # Grants belonging to other items of the document being stored (multi-grant documents).
    exclude: Collection[uuid.UUID] = ()


@dataclass
class Match:
    grant: Grant | None = None
    method: str | None = None  # "source_url", "fingerprint" or "fuzzy"
    score: float | None = None
    link: GrantSource | None = None  # the existing (source, url, item_key) link, if any
    possible_duplicates: list[tuple[Grant, float]] = field(default_factory=list)


def _other_document_grants(session: Session, query: MatchQuery) -> set[uuid.UUID]:
    return set(
        session.scalars(
            select(GrantSource.grant_id).where(
                GrantSource.source_id == query.source_id, GrantSource.url != query.url
            )
        )
    )


def find_match(
    session: Session,
    query: MatchQuery,
    *,
    match_threshold: float,
    possible_threshold: float,
    date_tolerance_days: int,
) -> Match:
    link = session.scalars(
        select(GrantSource).where(
            GrantSource.source_id == query.source_id,
            GrantSource.url == query.url,
            GrantSource.item_key == query.item_key,
        )
    ).one_or_none()
    if link is not None:
        return Match(link.grant, "source_url", 100.0, link)

    same_source = _other_document_grants(session, query)
    excluded = same_source | set(query.exclude)
    possible: dict[uuid.UUID, tuple[Grant, float]] = {}

    for grant in session.scalars(select(Grant).where(Grant.fingerprint == query.fingerprint)):
        if grant.id in query.exclude:
            continue
        if grant.id in same_source:
            possible[grant.id] = (grant, 100.0)
            continue
        return Match(grant, "fingerprint", 100.0)

    if query.funder_id is None:
        return Match(possible_duplicates=list(possible.values())[:MAX_POSSIBLE])

    best: tuple[Grant, float] | None = None
    for grant in session.scalars(select(Grant).where(Grant.funder_id == query.funder_id)):
        if grant.id in query.exclude or not dates_compatible(
            grant.closing_date, query.closing_date, date_tolerance_days
        ):
            continue
        if grant.id in excluded:
            continue
        score = float(fuzz.token_set_ratio(query.normalised_title, grant.normalised_title))
        if score >= match_threshold:
            if best is None or score > best[1]:
                best = (grant, score)
        elif score >= possible_threshold and grant.id not in possible:
            possible[grant.id] = (grant, score)

    ranked = sorted(possible.values(), key=lambda p: -p[1])
    if best is not None:
        ranked = [p for p in ranked if p[0].id != best[0].id]
        return Match(best[0], "fuzzy", best[1], possible_duplicates=ranked[:MAX_POSSIBLE])
    return Match(possible_duplicates=ranked[:MAX_POSSIBLE])
