"""FastAPI application entrypoint (API layer only).

Endpoints
---------
* ``GET /health`` - liveness plus non-secret configuration (never a credential).
* ``POST /assess`` - resolve the route, fetch every source concurrently, fuse,
  persist the record and return one :class:`AssessmentResponse`.
* ``GET /evidence/{assessment_id}`` - the stored record behind an assessment:
  the three source envelopes, the exact fusion prompt, the raw answer and the
  token usage.

Status codes, and why
---------------------
* ``422`` - the route is not a supported airport/city. Nothing was fetched and no
  quota was spent.
* ``500`` - the assessment record could not be written. The answer is withheld
  because an untraceable assessment is not a deliverable (``.clinerules`` rule 5).
* ``404`` - no stored record for that id (unknown, or not a UUID).
* ``200`` with ``status="error"``/``"partial"`` - a *successful* explanation of a
  failed or incomplete assessment. The envelope is the answer, and it says which
  sources are missing and why.

The data source and fusion layers are implemented; this module only wires them,
so it contains no vendor parsing and no business rules (``.clinerules`` rule 6).

Run locally (from the project root)::

    uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

import config
from backend.assess import (
    AssessmentNotFoundError,
    AssessmentRecordError,
    AssessmentStoreError,
    assess as run_assessment,
    load_assessment,
)
from backend.models import AssessmentResponse, TravelQuery
from data_sources import base, iata_lookup

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    description=(
        "Assesses travel disruption risk for a US route and date using real "
        "external data sources (Aviationstack, NWS, TomTom), AI signal fusion, "
        "and a persisted evidence record per assessment."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health", tags=["system"], summary="Liveness and configuration status")
async def health() -> Dict[str, Any]:
    """Report process health plus which credentials are still missing.

    This endpoint never reports fake component readiness: a component is ``ok`` only
    when it is implemented, and the fusion component is ``ok`` only when a model name
    and a usable key are configured.
    """
    missing = config.missing_credentials()
    fusion = config.fusion_readiness()
    return {
        "status": config.STATUS_OK,
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "schema_version": config.SCHEMA_VERSION,
        "settings": config.as_dict(),
        "missing_credentials": missing,
        "components": {
            "api_layer": "ok",
            "raw_response_logging": (
                "configured" if config.LOG_RAW_RESPONSES else "disabled"
            ),
            # Implemented: each source reports its own status per request, and a
            # missing key becomes that source's explicit error envelope.
            "data_sources": "ok",
            "ai_fusion": "ok" if fusion["ready"] else "unconfigured",
            "assessment_store": "ok",
        },
    }



class _InvalidRoute(ValueError):
    """The requested route is not something this app can assess."""


def _airports_named(text: str) -> List[iata_lookup.AirportInfo]:
    """Every supported airport whose served city or name is exactly ``text``."""
    wanted = " ".join(text.split()).lower()
    matches: List[iata_lookup.AirportInfo] = []
    for code in iata_lookup.supported_airports():
        info = iata_lookup.lookup_airport(code)
        if info is None:  # pragma: no cover - the table is static and consistent
            continue
        if wanted in (info.city.lower(), info.name.lower()):
            matches.append(info)
    return matches


def _endpoint_for(raw: str, role: str) -> base.Endpoint:
    """Resolve one end of the route to an audited airport record.

    A three-letter IATA code in the static table wins outright; otherwise an exact
    (case-insensitive) city or airport name is accepted when it identifies exactly
    one airport. Anything else -- an unknown code, an ambiguous city, a typo -- is
    rejected with an explicit message naming what was asked for. Nothing is guessed
    and no other airport is substituted.
    """
    text = " ".join((raw or "").split())
    if not text:
        raise _InvalidRoute(
            "{0} is empty. Give a supported US IATA code, e.g. JFK.".format(role)
        )
    code = text.upper()
    info = iata_lookup.lookup_airport(code) if len(code) == 3 else None
    if info is None:
        matches = _airports_named(text)
        if len(matches) > 1:
            raise _InvalidRoute(
                "{0} {1!r} matches more than one airport ({2}); give an IATA "
                "code.".format(
                    role, raw, ", ".join(sorted(item.iata for item in matches))
                )
            )
        if not matches:
            raise _InvalidRoute(
                "{0} {1!r} is not a supported airport or city. Supported airport "
                "codes: {2}.".format(
                    role, raw, ", ".join(iata_lookup.supported_airports())
                )
            )
        info = matches[0]
    return base.Endpoint(
        iata=info.iata,
        name=info.name,
        city=info.city,
        latitude=info.latitude,
        longitude=info.longitude,
    )


def _location_for(query: TravelQuery) -> base.Location:
    """Build the validated route, or raise ``_InvalidRoute`` naming the bad end."""
    origin = _endpoint_for(query.origin, "Origin")
    destination = _endpoint_for(query.destination, "Destination")
    if origin.iata == destination.iata:
        raise _InvalidRoute(
            "Origin and destination are the same airport ({0}); a route needs two "
            "different airports.".format(origin.iata)
        )
    return base.Location(origin=origin, destination=destination)


@app.post(
    "/assess",
    response_model=AssessmentResponse,
    tags=["assess"],
    summary="Assess travel disruption risk for a route and date",
    responses={
        422: {
            "description": (
                "The route is not a supported airport or city. Nothing was fetched."
            )
        },
        500: {
            "description": (
                "The assessment record could not be written, so the assessment was "
                "not returned."
            )
        },
    },
)
async def assess_route(query: TravelQuery) -> AssessmentResponse:
    """Run one assessment: fetch, fuse, persist, return.

    ``status="error"`` or ``"partial"`` inside a 200 response is deliberate: the
    envelope is a truthful answer about what could and could not be checked, and the
    caller can see the per-source reasons rather than a bare HTTP failure.
    """
    try:
        location = _location_for(query)
    except _InvalidRoute as exc:
        logger.info("POST /assess rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    logger.info(
        "POST /assess %s %s", location.route_label, query.travel_date.isoformat()
    )
    try:
        return await run_assessment(location, query.travel_date)
    except AssessmentStoreError as exc:
        logger.error("POST /assess could not persist its record: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


@app.get(
    "/assessment/{assessment_id}",
    response_model=AssessmentResponse,
    tags=["assess"],
    summary="Read the persisted assessment behind an id",
    responses={
        404: {"description": "No assessment was persisted under that id."},
        500: {
            "description": (
                "The stored record could not be read, carries no assessment, or no "
                "longer matches the response contract."
            )
        },
    },
)
async def stored_assessment(assessment_id: str) -> AssessmentResponse:
    """Return the assessment that was stored under ``assessment_id``.

    This reads the persisted record and re-validates its envelope; it never re-runs
    collection or fusion, so a demo can replay a real assessment without spending
    quota. A record written by a different build that no longer matches the contract
    is reported as such -- it is not silently re-shaped into something that looks
    valid. ``GET /evidence/{id}`` returns the whole record; this returns the envelope
    the frontend renders.
    """
    try:
        record = load_assessment(assessment_id)
    except AssessmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except AssessmentRecordError as exc:
        logger.error("GET /assessment could not read a record: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    stored = record.get("assessment")
    if not isinstance(stored, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The stored record for {0} carries no assessment object, so there is "
                "nothing to return. The record itself is still readable at "
                "GET /evidence/{0}.").format(assessment_id),
        )

    payload = dict(stored)
    # The store's key is authoritative for identity, not the copied envelope.
    payload["assessment_id"] = record.get("assessment_id") or assessment_id
    try:
        return AssessmentResponse.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The stored assessment for {0} does not match the current response "
                "contract ({1} validation error(s); first at {2!r}). It was written by "
                "a different build, so it is reported rather than re-shaped."
            ).format(
                assessment_id,
                exc.error_count(),
                (exc.errors()[0].get("loc") or ("body",))[0],
            ),
        ) from exc


@app.get(
    "/evidence/{assessment_id}",
    tags=["evidence"],
    summary="Read the stored record behind an assessment id",
)
async def evidence(assessment_id: str) -> Dict[str, Any]:
    """Return the persisted record: envelopes, fusion prompt/response and usage.

    The record is exactly what was written at assessment time, plus the path it was
    read from; the raw fusion body is truncated for display only and
    ``fusion_response_body_truncated`` says whether it was.
    """
    try:
        return load_assessment(assessment_id)
    except AssessmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except AssessmentRecordError as exc:
        logger.error("GET /evidence could not read a record: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

