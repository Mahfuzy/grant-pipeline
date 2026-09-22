"""M4 acceptance checks through the runner: no duplicates on re-run, a changed deadline
is recorded and reviewed, a grant listed by two sources is merged."""

import copy
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fundscout.config import Settings
from fundscout.db.models import (
    Grant,
    GrantChange,
    GrantSource,
    ReviewItem,
    ReviewReason,
    Source,
    SourceType,
)
from fundscout.fetch.storage import LocalRawStorage
from fundscout.pipeline.extraction import reprocess_source
from fundscout.taxonomy.loader import seed_taxonomy
from tests.db.test_extraction import llm_factory
from tests.db.test_runner import GrantsGovWeb, add_source, run
from tests.fakellm import FakeClient, llm_grant

NIH_TITLE = (
    "Feasibility Clinical Trials of Mind and Body Interventions for NCCIH High Priority "
    "Research Topics (R34 Clinical Trial Required)"
)


def grant_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Grant)) or 0


async def test_rerun_and_reprocess_create_no_duplicates(
    db_session: Session, tmp_path: Path
) -> None:
    seed_taxonomy(db_session)
    source = add_source(db_session, "grants_gov")
    source.config = {**source.config, "refetch_unchanged_after_days": None}
    db_session.commit()
    web = GrantsGovWeb()

    first = await run(db_session, "grants_gov", web, tmp_path)
    assert first.grants_created == 3 and grant_count(db_session) == 3
    await run(db_session, "grants_gov", web, tmp_path)
    for _ in range(2):
        await reprocess_source(
            db_session,
            source,
            storage=LocalRawStorage(tmp_path / "raw"),
            settings=Settings(),
            llm_factory=None,
        )
    assert grant_count(db_session) == 3
    assert db_session.scalar(select(func.count()).select_from(GrantSource)) == 3
    fields = set(db_session.scalars(select(GrantChange.field)))
    assert fields == {"created"}


async def test_changed_deadline_is_recorded_and_reviewed(
    db_session: Session, tmp_path: Path
) -> None:
    seed_taxonomy(db_session)
    source = add_source(db_session, "grants_gov")
    source.config = {**source.config, "refetch_unchanged_after_days": None}
    db_session.commit()
    web = GrantsGovWeb()
    await run(db_session, "grants_gov", web, tmp_path)

    changed = copy.deepcopy(web.details["357305"][0])
    changed["data"]["synopsis"]["responseDateStr"] = "2026-12-15-00-00-00"
    web.details["357305"] = [changed]
    second = await run(db_session, "grants_gov", web, tmp_path)
    assert second.grants_updated == 1 and second.grants_created == 0

    grant = db_session.scalars(select(Grant).where(Grant.title == NIH_TITLE)).one()
    change = db_session.scalars(
        select(GrantChange).where(GrantChange.field == "closing_date")
    ).one()
    assert (change.grant_id, change.old_value, change.new_value) == (
        grant.id,
        "2026-11-17",
        "2026-12-15",
    )
    assert change.source_run_id == second.id
    item = db_session.scalars(
        select(ReviewItem).where(ReviewItem.reason == ReviewReason.DEADLINE_CHANGED)
    ).one()
    assert item.grant_id == grant.id and grant.needs_review


MIRROR = "https://mirror.example.org"
FEED = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test mirror</title>
<item><title>NIH mind and body trials</title><link>{MIRROR}/grants/r34</link></item>
</channel></rss>"""


async def test_grant_listed_in_two_sources_is_merged(db_session: Session, tmp_path: Path) -> None:
    seed_taxonomy(db_session)
    add_source(db_session, "grants_gov")
    # A second, non-primary test source (RSS + LLM extraction) listing one of the grants.
    db_session.add(
        Source(
            name="mirror",
            adapter="generic_rss",
            base_url=MIRROR,
            source_type=SourceType.RSS,
            config={"feed_url": f"{MIRROR}/feed.xml"},
            terms_reviewed=True,
        )
    )
    db_session.commit()
    web = GrantsGovWeb()
    web.add(f"{MIRROR}/feed.xml", FEED, headers={"Content-Type": "application/rss+xml"})
    web.add(f"{MIRROR}/grants/r34", "<main><h1>NIH R34</h1></main>")
    client = FakeClient(
        [
            {
                "grants": [
                    llm_grant(
                        title=NIH_TITLE.upper(),
                        funder_name="National Institutes of Health (NIH)",
                        closing_date="2026-11-17",
                        deadline_type="fixed",
                        contact_url=f"{MIRROR}/contact",
                    )
                ]
            }
        ]
    )

    await run(db_session, "grants_gov", web, tmp_path)
    mirror = await run(db_session, "mirror", web, tmp_path, llm_factory=llm_factory(client))
    assert mirror.grants_created == 0 and mirror.grants_updated == 1
    assert grant_count(db_session) == 3

    grant = db_session.scalars(select(Grant).where(Grant.title == NIH_TITLE)).one()
    assert {(s.url, s.is_primary) for s in grant.sources} == {
        ("https://www.grants.gov/search-results-detail/357305", True),
        (f"{MIRROR}/grants/r34", False),
    }
    assert grant.contact_url == f"{MIRROR}/contact"  # filled from the second source


async def test_missing_counts_only_on_trustworthy_listings(
    db_session: Session, tmp_path: Path
) -> None:
    seed_taxonomy(db_session)
    add_source(db_session, "grants_gov")
    web = GrantsGovWeb()
    await run(db_session, "grants_gov", web, tmp_path)
    hits = web.search_data["data"]["oppHits"]

    def missing_counts() -> list[int]:
        return sorted(gs.missing_count for gs in db_session.scalars(select(GrantSource)))

    # The listing collapses to a third of its size: not trusted, nothing counted.
    web.search_data["data"]["oppHits"] = hits[:1]
    await run(db_session, "grants_gov", web, tmp_path)
    assert missing_counts() == [0, 0, 0]

    # A normal listing with one grant gone: counted as missing.
    web.search_data["data"]["oppHits"] = hits[:2]
    await run(db_session, "grants_gov", web, tmp_path)
    assert missing_counts() == [0, 0, 1]
