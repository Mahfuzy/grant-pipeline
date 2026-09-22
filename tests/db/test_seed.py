from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fundscout.db.models import GrantType, OrgType, Region, RegionCountry, Theme
from fundscout.taxonomy.loader import DATA_DIR, TAXONOMIES, seed_taxonomy


def counts(session: Session) -> dict[str, int]:
    return {
        m.__tablename__: session.scalar(select(func.count()).select_from(m)) or 0
        for m in (Theme, Region, OrgType, GrantType, RegionCountry)
    }


def test_seed_populates_and_is_idempotent(db_session: Session) -> None:
    first = seed_taxonomy(db_session)
    assert first["themes"].created == 20
    assert first["org_types"].created == 10
    assert first["grant_types"].created == 8
    after_first = counts(db_session)

    second = seed_taxonomy(db_session)
    assert all(r.created == 0 and r.updated == 0 for r in second.values())
    assert counts(db_session) == after_first


def test_region_hierarchy_and_countries(db_session: Session) -> None:
    seed_taxonomy(db_session)
    regions = {r.slug: r for r in db_session.scalars(select(Region))}
    assert regions["west-africa"].parent_id == regions["sub-saharan-africa"].id
    assert regions["sub-saharan-africa"].parent_id == regions["africa"].id
    africa = {c.country_code for c in regions["africa"].countries}
    west = {c.country_code for c in regions["west-africa"].countries}
    assert "GH" in west and west < africa


def test_seed_applies_yaml_edits(db_session: Session, tmp_path: Path) -> None:
    seed_taxonomy(db_session)
    for kind in TAXONOMIES:
        (tmp_path / f"{kind}.yaml").write_text((DATA_DIR / f"{kind}.yaml").read_text())
    themes = tmp_path / "themes.yaml"
    themes.write_text(
        themes.read_text().replace("label: Energy", "label: Energy Access")
        + "- slug: clean-cooking\n  label: Clean Cooking\n  parent: energy\n"
    )
    regions = tmp_path / "regions.yaml"
    regions.write_text(regions.read_text().replace('"GH", ', "", 1))

    result = seed_taxonomy(db_session, tmp_path)
    assert result["themes"].created == 1
    assert result["themes"].updated == 1
    assert result["regions"].updated > 0

    theme_rows = {t.slug: t for t in db_session.scalars(select(Theme))}
    assert theme_rows["energy"].label == "Energy Access"
    assert theme_rows["clean-cooking"].parent_id == theme_rows["energy"].id
    region_rows = {r.slug: r for r in db_session.scalars(select(Region))}
    for slug in ("west-africa", "sub-saharan-africa", "africa"):
        assert "GH" not in {c.country_code for c in region_rows[slug].countries}
    # global-south lists GH itself, so it is unaffected.
    assert "GH" in {c.country_code for c in region_rows["global-south"].countries}


def test_seed_reports_rows_missing_from_yaml(db_session: Session) -> None:
    db_session.add(Theme(slug="legacy-theme", label="Legacy"))
    db_session.flush()
    result = seed_taxonomy(db_session)
    assert result["themes"].not_in_yaml == ["legacy-theme"]
    assert db_session.scalars(select(Theme).where(Theme.slug == "legacy-theme")).one()
