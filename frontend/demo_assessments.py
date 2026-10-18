"""The three pre-run assessments offered as demo buttons in the UI.

An entry is an identifier plus a label -- an assessment id, the route, and the travel
date it was assessed for. **No result is stored here.** The button reads the record
back through ``GET /assessment/{id}``, so what the page shows is exactly what the
backend persisted, including any source that was down at the time. Fabricating an
envelope here would defeat the point of the evidence layer.

These ids come from real runs against the live sources and the fusion model on
2026-10-17 (see ``DEMO_ASSESSMENTS.md`` for the summaries). The records live in the
backend's ``logs/assessments/`` directory, which is git-ignored: on a machine that
does not have those files the button surfaces the backend's 404 -- naming the id --
instead of an invented assessment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class DemoAssessment:
    """One stored assessment, referred to by id.

    ``travel_date`` is the ISO date the assessment was *for* (what the user would have
    typed into the form), not the moment it was run: the label derived from it must
    stay truthful after the fact.
    """

    assessment_id: str
    origin: str
    destination: str
    travel_date: str


DEMO_ASSESSMENTS: Tuple[DemoAssessment, ...] = (
    DemoAssessment(
        assessment_id="4044a5aa-71e9-4f50-8014-64173c85bf08",
        origin="JFK",
        destination="LAX",
        travel_date="2026-10-17",
    ),
    DemoAssessment(
        assessment_id="147c9cbf-a85c-48b9-9389-d61d9ae5be9e",
        origin="SFO",
        destination="JFK",
        travel_date="2026-10-18",
    ),
    DemoAssessment(
        assessment_id="dc3004c6-5997-413c-970f-6eaef5a80319",
        origin="DEN",
        destination="MIA",
        travel_date="2026-10-17",
    ),
)
