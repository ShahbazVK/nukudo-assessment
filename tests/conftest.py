"""Shared test inputs: real captured payloads replayed through the real fetchers.

Why this file exists
--------------------
The fusion layer is tested against *real* ``NormalizedSignals`` envelopes, and
``.clinerules`` rule 3 forbids fabricating one. So instead of writing a hand-made
envelope, the ``captured_envelopes`` fixture takes the verbatim payloads in
``tests/fixtures/`` (captured from the live APIs on 2026-10-16/17 UTC - see
``tests/fixtures/README.md`` for provenance) and replays them through the **real**
``fetch_signals`` implementations. Only ``base.get_json`` is replaced, exactly as
``test_nws.py`` / ``test_tomtom.py`` / ``test_aviationstack.py`` already do. Every
signal, metric, evidence id and raw-response path in the result is therefore produced
by our own parsing code from a payload the vendors actually served.

The travel date is the one the captured flight board covers (2026-10-17), which is what
keeps all three sources on their real-time paths (TomTom accepts today/tomorrow only;
Aviationstack's real-time board is today). It comes from the payload rather than the
wall clock so the replay means the same thing tomorrow, and ``base.utc_now()`` is pinned
to noon UTC on that date so "today" is one date for every module whatever the local
clock says. Two consequences, both deliberate:

* Aviationstack only matches flights whose own ``flight_date`` is that date. At capture
  time that was 9 rows of the 100 returned; if a future capture has none, the envelope
  correctly becomes ``unavailable`` instead of an invented one.
* No payload is edited to make a test pass. If a replayed source has nothing to say on
  that date, the envelope says so and fusion is expected to handle it.

Nothing here asserts anything; the fixtures are inputs only.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

import config
from data_sources import aviationstack, base, nws, tomtom

FIXTURES: Path = Path(__file__).parent / "fixtures"

# The route every replayed capture belongs to: the real TomTom and Aviationstack
# captures are JFK->LAX incident/flight boards.
REPLAY_ORIGIN: str = "JFK"
REPLAY_DESTINATION: str = "LAX"


def _capture_travel_date() -> datetime.date:
    """The date the captured flight board was taken for (its newest ``flight_date``).

    The payloads are frozen captures, so the replay date comes from them rather than
    from the wall clock: a fixture whose meaning changes at midnight -- or when the
    suite is run the next day -- is not a fixture. If the board has no usable
    ``flight_date`` the wall clock date is used, and the replay degrades honestly to
    an ``unavailable`` Aviationstack envelope (see the module docstring).
    """
    payload = json.loads(
        (FIXTURES / "aviationstack_flights_JFK_LAX.json").read_text(encoding="utf-8")
    )
    dates: List[datetime.date] = []
    for row in payload.get("data") or ():
        if not isinstance(row, Mapping):
            continue
        try:
            dates.append(datetime.date.fromisoformat(row["flight_date"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not dates:
        return datetime.date.today()
    return max(dates)


def _fixture(name: str) -> Any:
    """A fixture document from ``tests/fixtures`` (see that directory's README)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_path(name: str) -> str:
    """Repo-relative path of a fixture, used as the recorded raw-evidence path."""
    return str((FIXTURES / name).relative_to(Path(config.PROJECT_ROOT)))


def _ok(payload: Any, url: str, raw_name: str) -> base.HttpResult:
    """A successful ``HttpResult`` carrying a real captured payload.

    ``raw_path`` points at the fixture file itself, so any evidence reference built
    from this envelope can actually be opened.
    """
    return base.HttpResult(
        ok=True,
        method="GET",
        url=url,
        status_code=200,
        latency_ms=12.0,
        attempts=1,
        payload=payload,
        raw_path=_fixture_path(raw_name),
        raw_paths=(_fixture_path(raw_name),),
        error=None,
        error_code=None,
        response_headers={"content-type": "application/json"},
        body_preview=None,
        content_type="application/json",
    )


class _ReplayTransport:
    """A ``base.get_json`` stand-in that serves captured payloads by URL/params.

    Routes are matched in order; the first predicate that matches wins. A request
    nothing matches is a test bug and raises immediately rather than returning
    ``None`` (which a source would have to report as a malformed payload).
    """

    def __init__(self, routes: List[Any]) -> None:
        self.routes = routes
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, source: str, url: str, **kwargs: Any) -> base.HttpResult:
        params: Mapping[str, Any] = kwargs.get("params") or {}
        self.calls.append({"source": source, "url": url, "params": dict(params)})
        for predicate, payload_name in self.routes:
            if predicate(url, params):
                return _ok(_fixture(payload_name), url, payload_name)
        raise AssertionError(
            "no captured payload matches this request: url={0!r} params={1!r}".format(
                url, dict(params)
            )
        )


def _ordered_transport(captures: List[str], url_marker: str) -> _ReplayTransport:
    """A transport that walks ``captures`` in call order, for ordered sources.

    TomTom queries one airport at a time in route order (origin, then destination),
    so its two requests map onto the JFK and LAX captures deterministically.
    """
    state = {"index": 0}

    class _Ordered(_ReplayTransport):
        async def __call__(
            self, source: str, url: str, **kwargs: Any
        ) -> base.HttpResult:
            params: Mapping[str, Any] = kwargs.get("params") or {}
            self.calls.append({"source": source, "url": url, "params": dict(params)})
            if url_marker not in url:
                raise AssertionError(
                    "unexpected {0} URL: {1!r}".format(source, url)
                )
            if state["index"] >= len(captures):
                raise AssertionError(
                    "more {0} requests than captured payloads".format(source)
                )
            name = captures[state["index"]]
            state["index"] += 1
            return _ok(_fixture(name), url, name)

    return _Ordered(routes=[])


def _nws_routes() -> List[Any]:
    """Route the three NWS calls per airport to that airport's real captures.

    The forecast URL is the one NWS itself returned in ``points``, so it is matched
    on its own path rather than reconstructed from the grid numbers.
    """

    def points_jfk(url: str, params: Mapping[str, Any]) -> bool:
        # The coordinates come from iata_lookup (the source's only location table),
        # which is what the fetched capture was requested with.
        return url.endswith("/points/40.639447,-73.779317")

    def points_lax(url: str, params: Mapping[str, Any]) -> bool:
        return url.endswith("/points/33.942501,-118.407997")

    def forecast_jfk(url: str, params: Mapping[str, Any]) -> bool:
        return "/gridpoints/OKX/42,40/forecast" in url

    def forecast_lax(url: str, params: Mapping[str, Any]) -> bool:
        return "/gridpoints/MTR/85,98/forecast" in url

    def alerts_jfk(url: str, params: Mapping[str, Any]) -> bool:
        return url.endswith("/alerts/active") and "40.639447" in str(
            params.get("point", "")
        )

    def alerts_lax(url: str, params: Mapping[str, Any]) -> bool:
        return url.endswith("/alerts/active") and "33.942501" in str(
            params.get("point", "")
        )

    return [
        (points_jfk, "nws_points_JFK.json"),
        (points_lax, "nws_points_SFO.json"),
        (forecast_jfk, "nws_forecast_JFK.json"),
        (forecast_lax, "nws_forecast_SFO.json"),
        (alerts_jfk, "nws_alerts_empty.json"),
        (alerts_lax, "nws_alerts_SFO.json"),
    ]


def captured_envelopes_for(
    monkeypatch: pytest.MonkeyPatch,
    *,
    origin: str = REPLAY_ORIGIN,
    destination: str = REPLAY_DESTINATION,
    travel_date: Optional[datetime.date] = None,
) -> Dict[str, base.NormalizedSignals]:
    """Replay every captured source through its real ``fetch_signals``.

    Returns ``{"aviationstack": envelope, "nws": envelope, "tomtom": envelope}``.
    Sentinel credentials are injected so the replay does not depend on the real
    ``.env`` (and never spends real quota); no payload is modified.
    """
    when = travel_date or _capture_travel_date()
    location = base.Location.from_iata(origin, destination)

    # Both real-time sources compare the travel date against ``base.utc_now()``, so the
    # clock is pinned to noon UTC on the capture's own date.  Without the pin the replay
    # falls off the real-time path whenever the local date and UTC disagree (any US
    # timezone after 19:00) and TomTom answers ``no_data_for_query``.
    def _pinned_utc_now() -> datetime.datetime:
        """Noon UTC on the replayed date -- so every source agrees it is "today"."""
        return datetime.datetime.combine(
            when, datetime.time(12, 0, tzinfo=datetime.timezone.utc)
        )

    monkeypatch.setattr(base, "utc_now", _pinned_utc_now)

    sentinel = "replay-" + uuid.uuid4().hex
    monkeypatch.setattr(config, "AVIATIONSTACK_API_KEY", sentinel, raising=False)
    monkeypatch.setattr(config, "TOMTOM_API_KEY", sentinel, raising=False)
    monkeypatch.setattr(
        config,
        "NWS_USER_AGENT",
        "(travel-disruption-assessor, replay@example.test)",
        raising=False,
    )

    aviationstack_transport = _ReplayTransport(
        routes=[
            (
                lambda url, params: url.endswith("/flights"),
                "aviationstack_flights_JFK_LAX.json",
            )
        ]
    )
    monkeypatch.setattr(aviationstack.base, "get_json", aviationstack_transport)
    aviationstack_envelope = asyncio.run(aviationstack.fetch_signals(location, when))

    nws_transport = _ReplayTransport(routes=_nws_routes())
    monkeypatch.setattr(nws.base, "get_json", nws_transport)
    nws_envelope = asyncio.run(nws.fetch_signals(location, when))

    tomtom_transport = _ordered_transport(
        ["tomtom_incidents_JFK.json", "tomtom_incidents_LAX.json"], "incidentDetails"
    )
    monkeypatch.setattr(tomtom.base, "get_json", tomtom_transport)
    tomtom_envelope = asyncio.run(tomtom.fetch_signals(location, when))

    return {
        "aviationstack": aviationstack_envelope,
        "nws": nws_envelope,
        "tomtom": tomtom_envelope,
    }


@pytest.fixture()
def captured_envelopes(
    monkeypatch: pytest.MonkeyPatch,
) -> Dict[str, base.NormalizedSignals]:
    """The three real envelopes for JFK->LAX today, replayed from real captures."""
    return captured_envelopes_for(monkeypatch)
