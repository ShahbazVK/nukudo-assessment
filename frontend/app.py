"""Streamlit frontend -- presentation only.

Sections, top to bottom: a collapsed "Demo assessments (pre-run)" expander, the input
form, the assessment, and a collapsible evidence drawer. This module renders state and
collects input; every network call goes through :mod:`frontend.api_client`
(``.clinerules`` rule 6). It does not call a third-party API, does not parse a vendor
payload, does not retry, and does not invent a value the backend did not return.

Run locally (backend on :8000), from the project root::

    streamlit run frontend/app.py
"""

from __future__ import annotations

import asyncio
import datetime
import html
import sys
import time
from pathlib import Path
from typing import Any, Coroutine, Dict, List, Optional, Tuple, TypeVar

# Make `config` / `frontend.*` / `backend.*` importable under `streamlit run`.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

import config  # noqa: E402
import frontend.api_client as api_client  # noqa: E402
import frontend.demo_assessments as demo_assessments  # noqa: E402
from backend.models import AssessmentResponse, EvidenceRef, SourceStatus  # noqa: E402
from data_sources import iata_lookup  # noqa: E402

T = TypeVar("T")

st.set_page_config(
    page_title="Travel Disruption Assessor",
    page_icon="✈️",
    layout="wide",
)

# --- Palette -----------------------------------------------------------------
# The risk pill is the one place custom HTML is justified: Streamlit has no
# coloured badge primitive. Every value injected into it is escaped.
_RISK_STYLES: Dict[str, Tuple[str, str]] = {
    "low": ("#1a7f37", "#ffffff"),
    "moderate": ("#d4a72c", "#1c1c1c"),
    "high": ("#d35400", "#ffffff"),
    "severe": ("#c62828", "#ffffff"),
    "unknown": ("#6b7280", "#ffffff"),
}
_STATUS_STYLES: Dict[str, str] = {
    "ok": "#1a7f37",
    "partial": "#d4a72c",
    "unavailable": "#9ca3af",
    "error": "#c62828",
}
_SOURCE_LABELS: Dict[str, str] = {
    "nws": "NWS",
    "aviationstack": "Aviationstack",
    "tomtom": "TomTom",
}

_STATE_KEYS: Tuple[str, ...] = (
    "response",
    "elapsed_seconds",
    "assess_error",
    "input_error",
    "evidence",
)


def _reset_state() -> None:
    """Create the session keys once, so every render reads a defined value."""
    for key in _STATE_KEYS:
        if key not in st.session_state:
            st.session_state[key] = {} if key == "evidence" else None


def _run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one async client call from Streamlit's synchronous script thread."""
    return asyncio.run(coroutine)


def _md_safe(value: object) -> str:
    """Escape characters that would let model-authored text restyle the page."""
    text = "" if value is None else str(value)
    for character in ("\\", "`", "*", "_", "[", "]", "#", "<"):
        text = text.replace(character, "\\" + character)
    return text


def _pill(
    label: str,
    background: str,
    colour: str,
    *,
    font_size: str = "1rem",
    tooltip: Optional[str] = None,
) -> str:
    """A coloured, rounded label. ``tooltip`` becomes a native hover title."""
    title = ' title="{0}"'.format(html.escape(tooltip, quote=True)) if tooltip else ""
    return (
        '<span{title} style="display:inline-block;padding:0.2rem 0.85rem;'
        "border-radius:999px;background:{background};color:{colour};"
        'font-size:{size};font-weight:600;line-height:1.7;white-space:nowrap;">'
        "{label}</span>"
    ).format(
        title=title,
        background=background,
        colour=colour,
        size=font_size,
        label=html.escape(label),
    )


def _source_label(source_id: str) -> str:
    """Human-readable source name, falling back to the id the backend sent."""
    return _SOURCE_LABELS.get(source_id, source_id)


def _validate_route(origin: str, destination: str) -> Optional[str]:
    """Client-side pre-check of the two codes, or ``None``.

    This is a convenience, not a business rule: ``POST /assess`` re-validates the
    route against the same table and stays authoritative. It exists so a typo costs
    a keystroke instead of three upstream calls and a model call.
    """
    supported = set(iata_lookup.supported_airports())
    codes: Dict[str, str] = {}
    for role, raw in (("Origin", origin), ("Destination", destination)):
        code = (raw or "").strip().upper()
        if not code:
            return "{0} is required: a 3-letter US IATA code, e.g. JFK.".format(role)
        if len(code) != 3 or not code.isalpha():
            return (
                "{0} {1!r} is not an IATA code. Use exactly three letters, e.g. "
                "JFK.".format(role, raw)
            )
        if code not in supported:
            return (
                "{0} {1!r} is not a supported US airport. Open 'Supported airports' "
                "below the form for the list, or try JFK, LAX, SFO or ORD.".format(
                    role, raw
                )
            )
        codes[role] = code
    if codes["Origin"] == codes["Destination"]:
        return (
            "Origin and destination are the same airport ({0}); a route needs two "
            "different airports.".format(codes["Origin"])
        )
    return None


def _render_form() -> Tuple[str, str, datetime.date, bool]:
    """Section 1: the input form, plus the list of airports it accepts."""
    with st.form("route_form"):
        columns = st.columns(3)
        origin: str = columns[0].text_input(
            "Origin airport (IATA)",
            max_chars=3,
            placeholder="JFK",
            help="Three-letter code of a supported US airport.",
        )
        destination: str = columns[1].text_input(
            "Destination airport (IATA)",
            max_chars=3,
            placeholder="LAX",
            help="Three-letter code of a supported US airport.",
        )
        travel_date: datetime.date = columns[2].date_input(
            "Travel date",
            value=datetime.date.today(),
            help="The date being assessed. Ground-incident data is real-time, so "
            "dates other than today are reported as unavailable, not guessed.",
        )
        submitted: bool = st.form_submit_button("Assess trip", type="primary")

    supported: List[str] = sorted(iata_lookup.supported_airports())
    with st.expander("Supported airports ({0})".format(len(supported))):
        st.caption(", ".join(supported))
    return origin, destination, travel_date, submitted


def _handle_submit(origin: str, destination: str, travel_date: datetime.date) -> None:
    """Validate, call the backend once, and store the outcome. Never retries."""
    state = st.session_state
    problem = _validate_route(origin, destination)
    if problem is not None:
        state["input_error"] = problem
        state["assess_error"] = None
        state["response"] = None
        state["elapsed_seconds"] = None
        return

    state["input_error"] = None
    state["assess_error"] = None
    state["response"] = None
    state["evidence"] = {}  # a new assessment gets a fresh evidence lookup

    started = time.perf_counter()
    with st.spinner(
        "Gathering signals from 3 sources and asking the AI to assess risk…"
    ):
        try:
            assessment = _run(
                api_client.assess(
                    origin.strip().upper(), destination.strip().upper(), travel_date
                )
            )
        except api_client.AssessError as exc:
            state["assess_error"] = exc
            state["elapsed_seconds"] = time.perf_counter() - started
            return
    state["response"] = assessment
    state["elapsed_seconds"] = time.perf_counter() - started


def _demo_date_label(travel_date: str) -> str:
    """``today`` / ``tomorrow`` when that is true *now*, else the ISO date.

    The demo entries were pre-run on a particular date, so the label is derived from
    the current date rather than hardcoded: a stale record must never be called
    "today".
    """
    try:
        parsed = datetime.date.fromisoformat(travel_date)
    except ValueError:
        return travel_date
    delta = (parsed - datetime.date.today()).days
    return {0: "today", 1: "tomorrow"}.get(delta, parsed.isoformat())


def _load_demo_assessment(entry: demo_assessments.DemoAssessment) -> None:
    """Read one pre-run assessment by id and make it the rendered one.

    Nothing is re-run: the backend returns the envelope it persisted. Latency is not
    shown for a load because this UI never measured it -- the stored record's timings
    are in the evidence drawer.
    """
    state = st.session_state
    state["input_error"] = None
    state["assess_error"] = None
    state["response"] = None
    state["elapsed_seconds"] = None
    state["evidence"] = {}
    with st.spinner("Reading the stored assessment from the backend…"):
        try:
            state["response"] = _run(api_client.get_assessment(entry.assessment_id))
        except api_client.AssessError as exc:
            state["assess_error"] = exc


def _render_demo_section() -> None:
    """Pre-run assessments: collapsed, above the form, so the blank form is the default.

    The buttons are a shortcut to something the backend already answered, not a second
    code path: each one calls ``GET /assessment/{id}`` and renders exactly what comes
    back.
    """
    if not demo_assessments.DEMO_ASSESSMENTS:
        return

    current = st.session_state.get("response")
    loaded_id = (
        current.assessment_id if isinstance(current, AssessmentResponse) else None
    )
    with st.expander("Demo assessments (pre-run)", expanded=False):
        st.caption(
            "Real assessments this backend already ran and persisted. Clicking one "
            "reads it back by id - no source is called, no model is called, nothing "
            "is regenerated. The form below is still the way to run a fresh one."
        )
        columns = st.columns(len(demo_assessments.DEMO_ASSESSMENTS))
        for column, entry in zip(columns, demo_assessments.DEMO_ASSESSMENTS):
            route = "{0} → {1}".format(entry.origin, entry.destination)
            label = "{0} · {1}".format(route, _demo_date_label(entry.travel_date))
            loaded = entry.assessment_id == loaded_id
            with column:
                clicked = st.button(
                    ("✓ " if loaded else "") + label,
                    key="demo_{0}".format(entry.assessment_id),
                    type="primary" if loaded else "secondary",
                    help=(
                        "On screen now."
                        if loaded
                        else (
                            "Reads the assessment persisted as {0} via "
                            "GET /assessment/{0}."
                        ).format(entry.assessment_id)
                    ),
                )
            if clicked:
                _load_demo_assessment(entry)
                # Re-render so the button's own "✓" state matches the panel below it;
                # without this the tick would only appear after the next interaction.
                st.rerun()


def _render_input_and_request_errors() -> None:
    """Show what went wrong, in the backend's own words, with no invented detail."""
    state = st.session_state
    if state["input_error"]:
        st.error(state["input_error"])
    error = state["assess_error"]
    if isinstance(error, api_client.AssessError):
        st.error(
            error.detail
            if error.status_code is None
            else "{0} (HTTP {1})".format(error.detail, error.status_code)
        )
        st.caption("Nothing was assessed: that message is the backend's, verbatim.")
        if isinstance(error.body, dict):
            with st.expander("Backend response (verbatim)"):
                st.json(error.body)

def _evidence_line(ref: EvidenceRef) -> str:
    """One traceable line: which source, which signal, which value."""
    field = ref.field or ref.evidence_id or "signal"
    value = ref.value if ref.value is not None else "not reported"
    line = "Source: **{0}** · Signal: {1} · Value: {2}".format(
        _md_safe(_source_label(ref.source)), _md_safe(field), _md_safe(value)
    )
    if ref.observed_at:
        line += " · Observed: {0}".format(_md_safe(ref.observed_at))
    return line


def _fusion_rate_limited(response: AssessmentResponse) -> bool:
    """True when the assessment is missing *because the provider rate-limited us*.

    The primary signal is the backend's own machine-readable reason,
    ``fusion_error_code == "rate_limited"``. When the field is absent (``None``) it
    came from a backend deployed before the field existed, so the verbatim-message
    heuristic is kept as a fallback -- but it is only consulted when the field is
    missing, never in addition to it: a code that says something else is
    authoritative.

    Both paths stay deliberately narrow: no risks were produced and no source
    failed, so a genuine per-source problem can never trigger the AI hint.
    """
    if response.status != config.STATUS_PARTIAL or response.risks:
        return False
    if any(item.status not in ("ok", "unavailable") for item in response.sources):
        return False
    if response.fusion_error_code is not None:
        return response.fusion_error_code == "rate_limited"
    text = " ".join([response.summary or ""] + list(response.uncertainty)).lower()
    return any(
        marker in text
        for marker in ("http 429", "rate limit", "rate-limit", "too many requests")
    )


def _render_status_banners(response: AssessmentResponse) -> None:
    """The one banner the situation deserves, in priority order."""
    if _fusion_rate_limited(response):
        st.warning(
            "The free-tier AI service is rate-limited to one call per minute. Wait "
            "~60 seconds and click **Assess trip** again - or expand **Show raw "
            "evidence and AI reasoning** below to read what the three sources found. "
            "Their signals were collected successfully and are shown further down; "
            "only the AI reasoning step was refused."
        )
    elif response.status == config.STATUS_PARTIAL:
        st.warning(
            "Some sources or the AI assessment were unavailable - see source status "
            "below. Everything shown here is what the backend actually returned."
        )


def _render_risk_badge(response: AssessmentResponse) -> None:
    """The 10-second read: how bad, how sure, and which route/date this is."""
    level = (response.risk_level or "unknown").lower()
    background, colour = _RISK_STYLES.get(level, _RISK_STYLES["unknown"])
    badge_column, detail_column = st.columns([1, 2], vertical_alignment="center")

    badge_column.markdown(
        _pill("{0} risk".format(level), background, colour, font_size="1.55rem"),
        unsafe_allow_html=True,
    )

    with detail_column:
        parts: List[str] = []
        if response.confidence is not None:
            parts.append("**Confidence {0:.2f}**".format(response.confidence))
        if response.confidence_label:
            parts.append(
                "{0} confidence".format(_md_safe(response.confidence_label).capitalize())
            )
        if parts:
            st.markdown(" · ".join(parts))
        else:
            st.caption("The model reported no confidence estimate for this assessment.")

        if response.status != config.STATUS_OK:
            st.markdown(
                _pill(
                    "status: {0}".format(response.status),
                    _STATUS_STYLES.get(response.status, "#6b7280"),
                    "#ffffff",
                    font_size="0.85rem",
                ),
                unsafe_allow_html=True,
            )

        facts: List[str] = []
        if response.origin and response.destination:
            facts.append(
                "{0} → {1}".format(
                    _md_safe(response.origin), _md_safe(response.destination)
                )
            )
        if response.travel_date:
            facts.append("travel date {0}".format(_md_safe(response.travel_date)))
        elapsed = st.session_state.get("elapsed_seconds")
        if isinstance(elapsed, float):
            facts.append("assessed in {0:.1f}s".format(elapsed))
        if facts:
            st.caption(" · ".join(facts))



def _render_risks(response: AssessmentResponse, evidence_url: str) -> None:
    """One card per risk, each with the evidence that supports it."""
    st.markdown("#### Primary risks")
    if not response.risks:
        st.caption(
            "The assessment did not report any individual risks."
            if response.status == config.STATUS_OK
            else "No risks were produced - see the reason above."
        )
        return

    for risk in response.risks:
        with st.container(border=True):
            title_column, chip_column = st.columns([4, 1], vertical_alignment="center")
            title_column.markdown("**{0}**".format(_md_safe(risk.risk)))
            severity = (risk.severity or "unknown").lower()
            background, colour = _RISK_STYLES.get(severity, _RISK_STYLES["unknown"])
            chip_column.markdown(
                _pill(severity.upper(), background, colour, font_size="0.85rem"),
                unsafe_allow_html=True,
            )

            detail: List[str] = []
            if risk.confidence is not None:
                detail.append("confidence {0:.2f}".format(risk.confidence))
            if risk.confidence_label:
                detail.append("{0} confidence".format(_md_safe(risk.confidence_label)))
            if detail:
                st.caption(" · ".join(detail))

            for ref in risk.evidence:
                st.markdown(_evidence_line(ref))
            if not risk.evidence:
                st.caption("No evidence reference was returned for this risk.")

            st.link_button(
                "View evidence ↗",
                evidence_url,
                help=(
                    "Opens GET /evidence/{0} in a new tab: the stored source "
                    "envelopes, the fusion prompt and the raw model answer."
                ).format(response.assessment_id),
            )


def _render_actions(response: AssessmentResponse) -> None:
    """What to do about it. The checkboxes are cosmetic and say so."""
    st.markdown("#### Recommended actions")
    if not response.recommended_actions:
        st.caption("The assessment did not recommend any actions.")
        return
    for index, action in enumerate(response.recommended_actions):
        st.checkbox(
            _md_safe(action),
            key="action_{0}_{1}".format(response.assessment_id, index),
            help="Cosmetic only: ticking this saves nothing and calls nothing.",
        )


_RAW_RECORDS_LABEL: str = "Raw records:"


def _code_span(value: str) -> str:
    """A path as inline code: monospace, and it cannot restyle the page.

    The path is machine-generated by the backend, so it is not escaped with
    ``_md_safe`` (which would leave visible backslashes inside a code span); a stray
    backtick, the only character that could break out of the span, is dropped.
    """
    return "`{0}`".format(str(value).replace("`", ""))


def _split_uncertainty_note(note: str) -> Tuple[List[str], List[str]]:
    """One uncertainty item -> (prose lines, raw-record paths).

    The backend discloses a prompt-size cap as one string: the sentence, then
    ``Raw records: <path>, <path>, ...``. Left on a single line the paths swamp the
    sentence, so the item is split for *rendering* only - the stored string is
    unchanged. An item with neither the marker nor a newline comes back as one prose
    line, which renders exactly as it did before.
    """
    text = (note or "").strip()
    if not text:
        return [], []
    paths: List[str] = []
    if _RAW_RECORDS_LABEL in text:
        text, _, raw = text.partition(_RAW_RECORDS_LABEL)
        paths = [chunk.strip().rstrip(".") for chunk in raw.split(",")]
        paths = [path for path in paths if path]
        if not paths:
            # Keep the disclosure even when there is no path to list.
            text = "{0} {1} not recorded".format(text.rstrip(), _RAW_RECORDS_LABEL)
    prose = [line.strip() for line in text.splitlines() if line.strip()]
    return prose, paths


def _render_uncertainty(response: AssessmentResponse) -> None:
    """What the assessment is *not* claiming - the honesty layer, always visible.

    A capped source says so in one string, ending with the raw records it drew from:
    that item renders as the sentence, a ``Raw records`` subheading and one monospace
    bullet per path, so a reader can verify every withheld signal. No path is
    truncated and none is invented. Items without the marker render as one bullet,
    exactly as before.
    """
    if not response.uncertainty:
        return
    st.markdown("#### What this assessment cannot tell you")
    for note in response.uncertainty:
        prose, paths = _split_uncertainty_note(note)
        for line in prose:
            st.markdown("- " + _md_safe(line))
        if paths:
            st.markdown("**Raw records:**")
            for path in paths:
                st.markdown("- " + _code_span(path))


def _render_sources(response: AssessmentResponse) -> None:
    """Three chips, so a missing source is impossible to miss."""
    st.markdown("#### Source status")
    if not response.sources:
        st.caption("The backend returned no per-source status records.")
        return

    columns = st.columns(len(response.sources))
    for column, source in zip(columns, response.sources):
        _render_source_chip(column, source)


def _render_source_chip(column: Any, source: SourceStatus) -> None:
    """One source: status chip, plus the failure code and reason when there is one.

    ``column`` is ``Any`` because Streamlit's ``DeltaGenerator`` is not exported from a
    public typing module; it is only used as a layout context manager here.
    """
    status = (source.status or "unknown").lower()
    hover = source.error_code or (", ".join(source.error_codes) or None)
    with column:
        st.markdown(
            _pill(
                "{0} · {1}".format(_source_label(source.source), status),
                _STATUS_STYLES.get(status, "#6b7280"),
                "#ffffff",
                tooltip=hover,
            ),
            unsafe_allow_html=True,
        )
        if source.error_code:
            st.caption("code: {0}".format(_md_safe(source.error_code)))
        if source.records is not None:
            st.caption("{0} record(s)".format(source.records))
        if source.error:
            st.caption(_md_safe(source.error))


def _evidence_record(
    assessment_id: str,
) -> Tuple[Optional[api_client.EvidenceRecord], Optional[str]]:
    """Fetch the stored record once per assessment id, or explain why it could not be.

    This is not a cache of assessments (each assessment is always a fresh call): it
    keeps the *same bytes* from being re-fetched on every widget rerun. It is cleared
    when a new assessment arrives.
    """
    cache: Dict[str, Any] = st.session_state.setdefault("evidence", {})
    cached = cache.get(assessment_id)
    if cached is not None:
        return cached, None
    try:
        record = _run(api_client.get_evidence(assessment_id))
    except api_client.AssessError as exc:
        return None, "The evidence record could not be read: {0}".format(exc)
    cache[assessment_id] = record
    return record, None


def _render_fusion_tab(record: api_client.EvidenceRecord) -> None:
    """The fourth tab: the exact prompt, the raw answer and what the call cost."""
    prompt = record.fusion_prompt or {}
    metadata = record.fusion_metadata or {}
    answer = record.fusion_response or {}

    model = prompt.get("model") or metadata.get("model") or "the model"
    st.markdown("**Prompt sent to {0}**".format(_md_safe(model)))
    messages: List[Dict[str, Any]] = list(prompt.get("messages") or [])
    if not messages:
        st.caption(
            "No prompt was stored: no model call was made for this assessment, so "
            "there is no reasoning to quote."
        )
    for message in messages:
        role = _md_safe(str(message.get("role", "message")).upper())
        st.markdown("_{0}_".format(role))
        st.code(str(message.get("content") or ""), language="text")

    st.markdown(
        "**Raw model answer** (HTTP {0}, {1} attempt(s))".format(
            answer.get("status_code") if answer.get("status_code") is not None else "n/a",
            answer.get("attempts") if answer.get("attempts") is not None else "n/a",
        )
    )
    st.code(str(answer.get("body_text") or "(no body was stored)"), language="json")
    if record.fusion_response_body_truncated:
        st.caption(
            "Truncated for display. The full body is in {0}.".format(
                record.assessment_record_path or "the assessment store"
            )
        )

    if metadata:
        parts: List[str] = []
        for label, key in (
            ("prompt", "prompt_tokens"),
            ("completion", "completion_tokens"),
            ("reasoning", "reasoning_tokens"),
            ("total", "total_tokens"),
        ):
            value = metadata.get(key)
            parts.append(
                "{0} {1}".format(label, "not reported" if value is None else value)
            )
        latency = metadata.get("latency_ms")
        if isinstance(latency, (int, float)):
            parts.append("latency {0:.0f}ms".format(latency))
        st.caption("tokens: " + " · ".join(parts))


def _render_evidence_drawer(response: AssessmentResponse) -> None:
    """Section 3: the record behind the answer, so the tool is not a black box."""
    with st.expander("Show raw evidence and AI reasoning"):
        record, problem = _evidence_record(response.assessment_id)
        if problem:
            st.warning(problem)
            return
        if record is None:
            # Unreachable: the failure path above returns. Kept so the type checker
            # sees the narrowing instead of an Optional flowing into the drawer.
            return
        st.caption(
            "Read from {0}. These are the persisted envelopes, prompt and answer - "
            "nothing here was regenerated for display.".format(
                record.assessment_record_path or "the assessment store"
            )
        )
        source_ids: List[str] = list(record.sources)
        tabs = st.tabs([_source_label(item) for item in source_ids] + ["AI reasoning"])
        for tab, source_id in zip(tabs, source_ids):
            with tab:
                st.caption(
                    "Normalized signals envelope for source id {0!r}.".format(source_id)
                )
                st.json(record.sources[source_id])
        with tabs[-1]:
            _render_fusion_tab(record)


def _render_assessment(response: AssessmentResponse) -> None:
    """Section 2: status, badge, summary, risks, actions, uncertainty, sources."""
    st.divider()
    _render_status_banners(response)
    evidence_url = "{0}/evidence/{1}".format(
        api_client.backend_url(), response.assessment_id
    )

    if response.status == config.STATUS_ERROR:
        st.subheader("Could not assess")
        if response.summary:
            st.markdown(_md_safe(response.summary))
    else:
        _render_risk_badge(response)
        if response.summary:
            st.markdown(_md_safe(response.summary))
        _render_risks(response, evidence_url)
        _render_actions(response)

    _render_uncertainty(response)
    _render_sources(response)
    _render_evidence_drawer(response)


def main() -> None:
    """Render the page: the pre-run demo buttons, the form, then the last result."""
    _reset_state()
    st.title("✈️ Travel Disruption Assessor")
    st.caption(
        "A US route and a date. One screen: how bad the trip looks, why - with the "
        "source and the value behind every claim - and what to do about it."
    )

    _render_demo_section()

    origin, destination, travel_date, submitted = _render_form()
    if submitted:
        _handle_submit(origin, destination, travel_date)

    _render_input_and_request_errors()

    response = st.session_state.get("response")
    if isinstance(response, AssessmentResponse):
        _render_assessment(response)
    elif not st.session_state.get("input_error") and st.session_state.get(
        "assess_error"
    ) is None:
        st.caption(
            "Nothing is fetched until you press **Assess trip** - or open a pre-run "
            "demo above."
        )


# Streamlit executes this script top to bottom on every interaction; there is no
# `if __name__ == "__main__"` guard to rely on, and AppTest relies on this call.
main()

