"""Internal API (M6): auth, grant search/filter/detail/patch, changes, sources, review."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fundscout.api.deps import get_session, settings_dep
from fundscout.api.main import create_app
from fundscout.api.routers.sources import source_runner
from fundscout.config import Settings
from fundscout.db.models import (
    Grant,
    GrantChange,
    ReviewItem,
    ReviewReason,
    RunStatus,
    SourceRun,
)
from fundscout.pipeline.store import store_grants
from fundscout.taxonomy.loader import seed_taxonomy
from tests.db.test_store import make_raw, make_source, result

KEY = {"X-API-Key": "test-key"}


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    seed_taxonomy(db_session)
    app = create_app(admin_ui_dir=None)
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[settings_dep] = lambda: Settings.model_validate(
        {"api_key": "test-key"}
    )
    app.state.runs = []

    async def fake_runner(name: str, force: bool, settings: Settings) -> None:
        app.state.runs.append((name, force))

    app.dependency_overrides[source_runner] = lambda: fake_runner
    with TestClient(app) as test_client:
        yield test_client


def add_grants(session: Session) -> dict[str, Any]:
    official = make_source(session, "funder", primary=True)

    def store(url: str, *results: Any) -> None:
        raw = make_raw(session, official, url)
        store_grants(
            session, official, raw, list(results), settings=Settings(), today=date(2026, 9, 21)
        )

    store("https://funder.example.org/health", result())
    store(
        "https://funder.example.org/climate",
        result(
            title="Climate Resilience Grants for Coastal Communities",
            description="Supports adaptation projects led by coastal community groups.",
            themes=["climate-environment"],
            countries=["Kenya"],
            closing_date="2027-01-15",
            amount_text="EUR 10,000 - 25,000",
        ),
    )
    store(
        "https://funder.example.org/old",
        result(title="Archive Fund 2025", closing_date="2025-05-01", themes=[], countries=[]),
    )
    session.commit()
    return {"source": official}


def test_auth(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/grants").status_code == 401
    assert client.get("/grants", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/grants", headers=KEY).status_code == 200


def test_api_refuses_without_configured_key(client: TestClient) -> None:
    app: Any = client.app
    app.dependency_overrides[settings_dep] = lambda: Settings()
    assert client.get("/grants", headers=KEY).status_code == 503


def test_grant_search_and_filters(client: TestClient, db_session: Session) -> None:
    add_grants(db_session)

    def titles(**params: Any) -> list[str]:
        response = client.get("/grants", params=params, headers=KEY)
        assert response.status_code == 200, response.text
        return [g["title"] for g in response.json()["items"]]

    assert len(titles()) == 3
    assert titles(status="open") == [
        "Community Health Innovation Fund",
        "Climate Resilience Grants for Coastal Communities",
    ]
    assert titles(status="closed") == ["Archive Fund 2025"]
    assert titles(theme="climate-environment") == [
        "Climate Resilience Grants for Coastal Communities"
    ]
    assert titles(country="KE") == ["Climate Resilience Grants for Coastal Communities"]
    assert titles(region="east-africa") == ["Climate Resilience Grants for Coastal Communities"]
    assert titles(region="west-africa") == ["Community Health Innovation Fund"]
    assert titles(q="adaptation coastal") == ["Climate Resilience Grants for Coastal Communities"]
    assert titles(q="innovation") == ["Community Health Innovation Fund"]
    assert titles(closing_after="2026-12-01") == [
        "Climate Resilience Grants for Coastal Communities"
    ]
    assert titles(closing_before="2026-12-01", status="open") == [
        "Community Health Innovation Fund"
    ]
    page = client.get("/grants", params={"page_size": 2, "page": 2}, headers=KEY).json()
    assert page["total"] == 3 and len(page["items"]) == 1


def test_grant_detail_and_manual_patch(client: TestClient, db_session: Session) -> None:
    add_grants(db_session)
    grant = db_session.scalars(
        select(Grant).where(Grant.title == "Community Health Innovation Fund")
    ).one()

    detail = client.get(f"/grants/{grant.id}", headers=KEY).json()
    assert detail["sources"][0]["is_primary"] is True
    assert detail["themes"] == ["health"] and detail["countries"] == ["GH"]
    assert detail["changes"][0]["field"] == "created"

    response = client.patch(
        f"/grants/{grant.id}",
        json={
            "closing_date": "2026-12-31",
            "themes": ["health", "research"],
            "changed_by": "reviewer",
        },
        headers=KEY,
    )
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["closing_date"] == "2026-12-31"
    assert sorted(patched["themes"]) == ["health", "research"]
    assert patched["manual_overrides"] == ["closing_date", "themes"]
    manual = db_session.scalars(
        select(GrantChange).where(GrantChange.change_source == "manual")
    ).all()
    assert {(c.field, c.changed_by) for c in manual} == {
        ("closing_date", "reviewer"),
        ("themes", "reviewer"),
    }

    bad = client.patch(f"/grants/{grant.id}", json={"themes": ["nonsense"]}, headers=KEY)
    assert bad.status_code == 422
    assert client.patch(f"/grants/{grant.id}", json={"title": None}, headers=KEY).status_code == 422
    assert (
        client.get("/grants/00000000-0000-0000-0000-000000000000", headers=KEY).status_code == 404
    )


def test_changes_since(client: TestClient, db_session: Session) -> None:
    add_grants(db_session)
    changes = client.get("/changes", params={"since": "2020-01-01T00:00:00Z"}, headers=KEY)
    body = changes.json()
    assert body["total"] == 3 and {c["field"] for c in body["items"]} == {"created"}
    assert body["items"][0]["grant_title"]
    future = client.get("/changes", params={"since": "2099-01-01T00:00:00Z"}, headers=KEY)
    assert future.json()["total"] == 0


def test_sources_and_manual_run(client: TestClient, db_session: Session) -> None:
    source = add_grants(db_session)["source"]
    db_session.add(
        SourceRun(source_id=source.id, status=RunStatus.SUCCESS, finished_at=datetime.now(UTC))
    )
    db_session.commit()

    [listed] = client.get("/sources", headers=KEY).json()
    assert listed["name"] == "funder" and listed["grant_count"] == 3
    assert listed["last_run"]["status"] == "success"
    runs = client.get(f"/sources/{source.id}/runs", headers=KEY).json()
    assert len(runs) == 1

    response = client.post(f"/sources/{source.id}/run", headers=KEY)
    assert response.status_code == 202
    app: Any = client.app
    assert app.state.runs == [("funder", False)]

    source.terms_reviewed = False
    db_session.commit()
    assert client.post(f"/sources/{source.id}/run", headers=KEY).status_code == 409
    assert client.post(f"/sources/{source.id}/run?force=true", headers=KEY).status_code == 202

    source.terms_reviewed = True
    db_session.add(SourceRun(source_id=source.id, status=RunStatus.RUNNING))
    db_session.commit()
    assert client.post(f"/sources/{source.id}/run", headers=KEY).status_code == 409


def test_review_queue_resolve_and_dismiss(client: TestClient, db_session: Session) -> None:
    source = make_source(db_session, "aggregator")
    raw = make_raw(db_session, source, "https://aggregator.example.org/1")
    store_grants(
        db_session,
        source,
        raw,
        [result(amount_text="up to $5,000"), result(title=None)],
        settings=Settings(),
        today=date(2026, 9, 21),
    )
    db_session.commit()

    listing = client.get("/review", headers=KEY).json()
    reasons = {i["reason"] for i in listing["items"]}
    assert reasons == {"ambiguous_currency", "missing_required_fields"}
    currency = next(i for i in listing["items"] if i["reason"] == "ambiguous_currency")
    assert currency["grant_title"] == "Community Health Innovation Fund"

    detail = client.get(f"/review/{currency['id']}", headers=KEY).json()
    assert detail["grant"]["title"] == "Community Health Innovation Fund"
    assert detail["raw_document"]["url"] == "https://aggregator.example.org/1"
    assert detail["grant"]["needs_review"] is True

    resolved = client.post(
        f"/review/{currency['id']}/resolve",
        json={"resolved_by": "reviewer", "note": "USD confirmed"},
        headers=KEY,
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["grant"]["needs_review"] is False
    assert client.post(f"/review/{currency['id']}/dismiss", headers=KEY).status_code == 409

    missing = next(i for i in listing["items"] if i["reason"] == "missing_required_fields")
    assert client.post(f"/review/{missing['id']}/dismiss", headers=KEY).status_code == 200
    assert client.get("/review", headers=KEY).json()["total"] == 0
    assert client.get("/review", params={"status": "resolved"}, headers=KEY).json()["total"] == 1
    assert (
        client.get(
            "/review", params={"reason": "ambiguous_currency", "status": "resolved"}, headers=KEY
        ).json()["total"]
        == 1
    )
    items = db_session.scalars(select(ReviewItem)).all()
    assert {i.reason for i in items} == {
        ReviewReason.AMBIGUOUS_CURRENCY,
        ReviewReason.MISSING_REQUIRED_FIELDS,
    }


def test_admin_ui_is_served(tmp_path: Any) -> None:
    (tmp_path / "index.html").write_text("<html>admin</html>")
    app = create_app(admin_ui_dir=tmp_path)
    with TestClient(app) as test_client:
        assert test_client.get("/admin/").text == "<html>admin</html>"
        assert test_client.get("/", follow_redirects=False).headers["location"] == "/admin/"


def test_taxonomy(client: TestClient) -> None:
    body = client.get("/taxonomy", headers=KEY).json()
    assert {"slug": "health", "label": "Health"} in body["themes"]
    assert set(body) == {"themes", "regions", "org_types", "grant_types"}
