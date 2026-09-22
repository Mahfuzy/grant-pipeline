"""EU Funding & Tenders Portal topic JSON (`.../data/topicDetails/<id>.json`).

A standard format shared by every EU programme on the portal, so it has its own
extractor rather than a per-source mapping. Budget data is keyed per call and lists
several topics' actions; the entries for this topic are selected by identifier.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from fundscout.db.models import ExtractionMethod
from fundscout.extract.base import ExtractionFailed, ExtractionInput, ExtractionOutput
from fundscout.extract.schema import CandidateGrant
from fundscout.normalise.dates import date_from_epoch_ms
from fundscout.normalise.taxonomy import default_taxonomy
from fundscout.normalise.text import html_to_text


class FtTopicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    funder_name: str | None = None
    funder_website: str | None = None


def _fmt(amount: Decimal) -> str:
    return f"EUR {amount:,.0f}"


class FtTopicExtractor:
    def __init__(self, config: FtTopicConfig):
        self.config = config

    def extract(self, doc: ExtractionInput) -> ExtractionOutput:
        try:
            topic = json.loads(doc.content)["TopicDetails"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ExtractionFailed(f"not an F&T topicDetails document: {exc!r}") from exc
        identifier: str = topic.get("identifier") or ""

        actions = topic.get("actions") or []
        opening: date | None = None
        deadlines: list[date] = []
        status = None
        for action in actions:
            opening = opening or date_from_epoch_ms(action.get("plannedOpeningDate"))
            deadlines += [
                d for d in map(date_from_epoch_ms, action.get("deadlineDates") or []) if d
            ]
            status = status or (action.get("status") or {}).get("description")
        deadlines = sorted(set(deadlines))

        budgets = self._topic_budget_entries(topic, identifier)
        minimum = [Decimal(str(b["minContribution"])) for b in budgets if b.get("minContribution")]
        maximum = [Decimal(str(b["maxContribution"])) for b in budgets if b.get("maxContribution")]
        amount_min = min(minimum) if minimum else None
        amount_max = max(maximum) if maximum else None
        amount_lines = []
        if amount_min is not None and amount_max is not None:
            amount_lines.append(
                f"{_fmt(amount_min)} per grant"
                if amount_min == amount_max
                else f"{_fmt(amount_min)} to {_fmt(amount_max)} per grant"
            )
        for b in budgets:
            total = sum(Decimal(str(v)) for v in (b.get("budgetYearMap") or {}).values())
            if total:
                amount_lines.append(f"topic budget {_fmt(total)}")
            if b.get("expectedGrants"):
                amount_lines.append(f"{b['expectedGrants']} grants expected")

        deadline_text = None
        if len(deadlines) > 1:
            deadline_text = "; ".join(
                f"Stage {i}: {d.isoformat()}" for i, d in enumerate(deadlines, start=1)
            )
        elif deadlines:
            deadline_text = deadlines[0].isoformat()

        taxonomy = default_taxonomy()
        labels = [*(topic.get("keywords") or []), *(topic.get("tags") or [])]
        themes = sorted({s for label in labels for s in taxonomy.resolve("themes", label)})

        candidate = CandidateGrant(
            title=topic.get("title"),
            funder_name=self.config.funder_name,
            funder_website=self.config.funder_website,
            description=html_to_text(topic.get("description")),
            amount_min=amount_min,
            amount_max=amount_max,
            currency="EUR" if amount_min is not None or amount_max is not None else None,
            amount_text="; ".join(amount_lines) or None,
            opening_date=opening,
            closing_date=deadlines[0] if deadlines else None,
            deadline_type="multiple" if len(deadlines) > 1 else None,
            deadline_text=deadline_text,
            themes=themes,
            eligibility_text=html_to_text(topic.get("conditions")),
            application_url=doc.url,
            field_confidence={
                name: 1.0
                for name in ("title", "amount_min", "amount_max", "opening_date", "closing_date")
            },
        )
        return ExtractionOutput(
            ExtractionMethod.API,
            [candidate],
            notes={"identifier": identifier, "portal_status": status},
        )

    @staticmethod
    def _topic_budget_entries(topic: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
        overview = topic.get("budgetOverviewJSONItem") or {}
        entries = [
            entry
            for actions in (overview.get("budgetTopicActionMap") or {}).values()
            for entry in actions
            if str(entry.get("action", "")).split(" - ")[0].strip().lower() == identifier.lower()
        ]
        return entries
