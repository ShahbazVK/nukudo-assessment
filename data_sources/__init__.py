"""Data source integrations.

Every *source* module in this package exposes exactly one public entry point::

    async def fetch_signals(location: Location, date: datetime.date) -> NormalizedSignals

and returns a normalized signal envelope (never a raw vendor payload).

``base`` and ``iata_lookup`` are the exception, and are not themselves sources:
they are the shared library the source modules are built from (envelope types,
error-state vocabulary, raw-response evidence logging, async HTTP helper, evidence
reader, and the static US airport coordinate table).

Modules
-------
- ``base``           shared contract, error-state vocabulary, raw-response
                     evidence logging, async HTTP helper (IMPLEMENTED)
- ``iata_lookup``    static IATA -> coordinates/timezone table for 58 US airports
                     (IMPLEMENTED; library code, not a source)
- ``nws``            National Weather Service active alerts + gridpoint forecast
                     (IMPLEMENTED)
- ``aviationstack``  flight status: delays, cancellations, diversions (IMPLEMENTED;
                     free tier = 100 requests/month, real-time flights only)
- ``tomtom``         road incidents within 15 km of each airport, real-time only
                     (IMPLEMENTED; v5 incidentDetails, bbox + distance filter)
- ``gdelt``          news and event signals (optional, later)

``nws`` is the first implemented vendor integration and the reference for the
pattern the rest follow: coordinates from ``iata_lookup``, every request through
``base.get_json`` (which logs raw evidence before parsing), mapping onto the
``config.RISK_LEVELS`` vocabulary, and an explicit error/unavailable envelope
whenever a piece of the answer is missing.
"""

from __future__ import annotations
