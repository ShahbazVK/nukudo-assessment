"""GDELT news/event signals - OPTIONAL, NOT IMPLEMENTED.

Deferred: the brief requires at least three real sources (Aviationstack, NWS,
TomTom). GDELT is a possible fourth source for news/event context, enabled via
``config.GDELT_ENABLED`` (default false).

Planned scope (separate prompt, only if time allows):

* Entry point: ``fetch_signals(location: Location, date: datetime.date) -> NormalizedSignals``
  (single public function, per .clinerules rule 4).
* Credential: none required. Base URL: ``config.GDELT_BASE_URL``.
* Planned signals: event/coverage counts and notable incidents (strikes, severe
  events, transport disruptions) local to the route endpoints, with article
  references that can be cited as evidence.
* Note: GDELT has no historic-by-date guarantee at prototype scale, so any
  shortfall must be reported as explicit uncertainty, never as zero risk.
"""

from __future__ import annotations
