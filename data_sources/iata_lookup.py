"""Static IATA -> coordinates lookup for the airports this app covers.

What this module is
-------------------
A deliberate, auditable table of US airports that a source module resolves an
:class:`~data_sources.base.Endpoint` against. It is *not* geocoding and it never
guesses:

* an unknown or non-US IATA code returns ``None``, and the caller must turn that
  into an explicit error/unavailable state naming the missing coverage;
* there is no city-name resolution, no fuzzy matching and no external lookup API.

Provenance (so any single row can be checked by hand)
-----------------------------------------------------
* ``latitude`` / ``longitude``: the OurAirports public dataset
  (``https://davidmegginson.github.io/ourairports-data/airports.csv``) - the rows
  whose ``iata_code`` matches and whose ``type`` is ``large_airport`` or
  ``medium_airport`` - rounded to 6 decimal places (about 0.1 m, i.e. precision
  far below what a forecast grid can use).
* ``name``: the dataset's ``name`` field, with en/em dashes normalised to ``-`` so
  the file stays plain ASCII.
* ``city`` / ``state``: the *served* city, from the dataset's ``municipality`` and
  ``iso_region``. NWS reports the *nearest* municipality in ``relativeLocation``,
  which is a different thing (it calls SFO "Burlingame" and DFW "Grapevine"), so
  that field is used for verification only.
* ``timezone``: the ``properties.timeZone`` NWS returns for the same point - the
  timezone NWS itself applies to that grid's forecast periods.

Every row was verified against the live keyless NWS ``/points/{lat},{lon}``
endpoint on 2026-10-17: all 58 points answered HTTP 200 with the expected state and
a timezone. ``tests/test_iata_lookup.py`` pins the table's invariants, and the live
test in ``tests/test_nws_live.py`` re-checks sample rows against NWS itself.

Scope
-----
58 US airports (including Alaska, Hawaii and Puerto Rico) and nothing else: a
non-US route is genuinely out of scope for the NWS source, which reports it as
``unavailable``/``no_data_for_query`` rather than pretending to cover it. A
production version of this app would replace this table with an imported, refreshed
airport database; the public signatures here would not change.

This module is shared library code, not a data source: like ``base``, it is exempt
from the "one public entry point per module" rule (.clinerules rule 4) because it
exposes the lookup helpers the source modules are built from.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional

__all__ = ["AirportInfo", "lookup_airport", "supported_airports"]

# The dataset is read as text, so a coordinate outside these bounds can only mean a
# mis-parsed row: the table then refuses to build rather than serve a wrong point.
_MIN_LATITUDE: float = -90.0
_MAX_LATITUDE: float = 90.0
_MIN_LONGITUDE: float = -180.0
_MAX_LONGITUDE: float = 180.0


@dataclasses.dataclass(frozen=True)
class AirportInfo:
    """One static airport record: served city, coordinates and NWS timezone.

    Frozen on purpose: the table is shared, read-only reference data, so no caller
    can edit a record in place and no lookup can be mutated into returning a made
    up location.
    """

    iata: str
    name: str
    city: str
    state: str
    latitude: float
    longitude: float
    timezone: str

    def __post_init__(self) -> None:
        """Normalise and validate the record when it is constructed."""
        cleaned = (self.iata or "").strip().upper()
        if len(cleaned) != 3 or not cleaned.isalpha():
            raise ValueError("AirportInfo.iata must be a 3-letter IATA code")
        object.__setattr__(self, "iata", cleaned)
        for field in ("name", "city", "state", "timezone"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "AirportInfo.{0} must be a non-empty string for {1}".format(
                        field, cleaned
                    )
                )
        for field in ("latitude", "longitude"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    "AirportInfo.{0} must be numeric for {1}".format(field, cleaned)
                )
        if not _MIN_LATITUDE <= self.latitude <= _MAX_LATITUDE:
            raise ValueError(
                "AirportInfo.latitude {0!r} is outside [{1}, {2}] for {3}".format(
                    self.latitude, _MIN_LATITUDE, _MAX_LATITUDE, cleaned
                )
            )
        if not _MIN_LONGITUDE <= self.longitude <= _MAX_LONGITUDE:
            raise ValueError(
                "AirportInfo.longitude {0!r} is outside [{1}, {2}] for {3}".format(
                    self.longitude, _MIN_LONGITUDE, _MAX_LONGITUDE, cleaned
                )
            )

    @property
    def coordinates(self) -> str:
        """``lat,lon`` at 6 decimals - the form NWS ``/points`` expects."""
        return "{0:.6f},{1:.6f}".format(self.latitude, self.longitude)

    def to_dict(self) -> Dict[str, object]:
        """Plain-dict form, used when the record is quoted as evidence."""
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- #
# The table. One row per airport, grouped by region so it can be eyeballed:
#   iata, official name, served city, state, latitude, longitude, NWS timezone
# Sources and the verification pass are documented in the module docstring above.
# --------------------------------------------------------------------------- #
_AIRPORTS: Dict[str, AirportInfo] = {
    "JFK": AirportInfo(iata="JFK", name="John F. Kennedy International Airport", city="New York", state="NY", latitude=40.639447, longitude=-73.779317, timezone="America/New_York"),
    "LGA": AirportInfo(iata="LGA", name="LaGuardia Airport", city="New York", state="NY", latitude=40.777199, longitude=-73.872597, timezone="America/New_York"),
    "EWR": AirportInfo(iata="EWR", name="Newark Liberty International Airport", city="Newark", state="NJ", latitude=40.6894, longitude=-74.170545, timezone="America/New_York"),
    "BOS": AirportInfo(iata="BOS", name="Boston Logan International Airport", city="Boston", state="MA", latitude=42.36197, longitude=-71.0079, timezone="America/New_York"),
    "PVD": AirportInfo(iata="PVD", name="Rhode Island T. F. Green International Airport", city="Providence/Warwick", state="RI", latitude=41.725038, longitude=-71.425668, timezone="America/New_York"),
    "BDL": AirportInfo(iata="BDL", name="Bradley International Airport", city="Hartford", state="CT", latitude=41.938555, longitude=-72.688016, timezone="America/New_York"),
    "BUF": AirportInfo(iata="BUF", name="Buffalo Niagara International Airport", city="Buffalo", state="NY", latitude=42.940498, longitude=-78.732201, timezone="America/New_York"),
    "PHL": AirportInfo(iata="PHL", name="Philadelphia International Airport", city="Philadelphia", state="PA", latitude=39.871899, longitude=-75.241096, timezone="America/New_York"),
    "BWI": AirportInfo(iata="BWI", name="Baltimore/Washington International Thurgood Marshall Airport", city="Baltimore", state="MD", latitude=39.1754, longitude=-76.668297, timezone="America/New_York"),
    "DCA": AirportInfo(iata="DCA", name="Ronald Reagan Washington National Airport", city="Washington", state="DC", latitude=38.8521, longitude=-77.037697, timezone="America/New_York"),
    "IAD": AirportInfo(iata="IAD", name="Washington Dulles International Airport", city="Dulles", state="VA", latitude=38.9445, longitude=-77.455803, timezone="America/New_York"),
    "ATL": AirportInfo(iata="ATL", name="Hartsfield Jackson Atlanta International Airport", city="Atlanta", state="GA", latitude=33.6367, longitude=-84.428101, timezone="America/New_York"),
    "MIA": AirportInfo(iata="MIA", name="Miami International Airport", city="Miami", state="FL", latitude=25.796011, longitude=-80.289751, timezone="America/New_York"),
    "FLL": AirportInfo(iata="FLL", name="Fort Lauderdale Hollywood International Airport", city="Fort Lauderdale", state="FL", latitude=26.072599, longitude=-80.152702, timezone="America/New_York"),
    "MCO": AirportInfo(iata="MCO", name="Orlando International Airport", city="Orlando", state="FL", latitude=28.429399, longitude=-81.308998, timezone="America/New_York"),
    "TPA": AirportInfo(iata="TPA", name="Tampa International Airport", city="Tampa", state="FL", latitude=27.9755, longitude=-82.533203, timezone="America/New_York"),
    "JAX": AirportInfo(iata="JAX", name="Jacksonville International Airport", city="Jacksonville", state="FL", latitude=30.492469, longitude=-81.687813, timezone="America/New_York"),
    "MSY": AirportInfo(iata="MSY", name="Louis Armstrong New Orleans International Airport", city="New Orleans", state="LA", latitude=29.993401, longitude=-90.264673, timezone="America/Chicago"),
    "CLT": AirportInfo(iata="CLT", name="Charlotte Douglas International Airport", city="Charlotte", state="NC", latitude=35.214001, longitude=-80.9431, timezone="America/New_York"),
    "RDU": AirportInfo(iata="RDU", name="Raleigh-Durham International Airport", city="Raleigh/Durham", state="NC", latitude=35.878659, longitude=-78.7873, timezone="America/New_York"),
    "BNA": AirportInfo(iata="BNA", name="Nashville International Airport", city="Nashville", state="TN", latitude=36.1245, longitude=-86.6782, timezone="America/Chicago"),
    "MEM": AirportInfo(iata="MEM", name="Frederick W. Smith International Airport", city="Memphis", state="TN", latitude=35.043845, longitude=-89.97634, timezone="America/Chicago"),
    "ORD": AirportInfo(iata="ORD", name="Chicago O'Hare International Airport", city="Chicago", state="IL", latitude=41.9786, longitude=-87.9048, timezone="America/Chicago"),
    "MDW": AirportInfo(iata="MDW", name="Chicago Midway International Airport", city="Chicago", state="IL", latitude=41.785999, longitude=-87.752403, timezone="America/Chicago"),
    "DTW": AirportInfo(iata="DTW", name="Detroit Metropolitan Wayne County Airport", city="Detroit", state="MI", latitude=42.21377, longitude=-83.353786, timezone="America/Detroit"),
    "MSP": AirportInfo(iata="MSP", name="Minneapolis-Saint Paul International Airport / Wold-Chamberlain Field", city="Minneapolis", state="MN", latitude=44.880081, longitude=-93.221741, timezone="America/Chicago"),
    "STL": AirportInfo(iata="STL", name="St. Louis Lambert International Airport", city="St Louis", state="MO", latitude=38.748697, longitude=-90.370003, timezone="America/Chicago"),
    "MCI": AirportInfo(iata="MCI", name="Kansas City International Airport", city="Kansas City", state="MO", latitude=39.301699, longitude=-94.713893, timezone="America/Chicago"),
    "MKE": AirportInfo(iata="MKE", name="General Mitchell International Airport", city="Milwaukee", state="WI", latitude=42.947201, longitude=-87.896599, timezone="America/Chicago"),
    "CLE": AirportInfo(iata="CLE", name="Cleveland Hopkins International Airport", city="Cleveland", state="OH", latitude=41.411701, longitude=-81.8498, timezone="America/New_York"),
    "CMH": AirportInfo(iata="CMH", name="John Glenn Columbus International Airport", city="Columbus", state="OH", latitude=39.998001, longitude=-82.891899, timezone="America/New_York"),
    "IND": AirportInfo(iata="IND", name="Indianapolis International Airport", city="Indianapolis", state="IN", latitude=39.7173, longitude=-86.294403, timezone="America/Indiana/Indianapolis"),
    "CVG": AirportInfo(iata="CVG", name="Cincinnati Northern Kentucky International Airport", city="Cincinnati/Covington", state="KY", latitude=39.048801, longitude=-84.667801, timezone="America/New_York"),
    "DFW": AirportInfo(iata="DFW", name="Dallas Fort Worth International Airport", city="Dallas-Fort Worth", state="TX", latitude=32.896801, longitude=-97.038002, timezone="America/Chicago"),
    "DAL": AirportInfo(iata="DAL", name="Dallas Love Field", city="Dallas", state="TX", latitude=32.844776, longitude=-96.847653, timezone="America/Chicago"),
    "IAH": AirportInfo(iata="IAH", name="George Bush Intercontinental Airport", city="Houston", state="TX", latitude=29.9844, longitude=-95.3414, timezone="America/Chicago"),
    "HOU": AirportInfo(iata="HOU", name="William P. Hobby Airport", city="Houston", state="TX", latitude=29.645336, longitude=-95.276812, timezone="America/Chicago"),
    "AUS": AirportInfo(iata="AUS", name="Austin Bergstrom International Airport", city="Austin", state="TX", latitude=30.197535, longitude=-97.662015, timezone="America/Chicago"),
    "SAT": AirportInfo(iata="SAT", name="San Antonio International Airport", city="San Antonio", state="TX", latitude=29.533701, longitude=-98.469803, timezone="America/Chicago"),
    "DEN": AirportInfo(iata="DEN", name="Denver International Airport", city="Denver", state="CO", latitude=39.860027, longitude=-104.673792, timezone="America/Denver"),
    "SLC": AirportInfo(iata="SLC", name="Salt Lake City International Airport", city="Salt Lake City", state="UT", latitude=40.78886, longitude=-111.979866, timezone="America/Denver"),
    "PHX": AirportInfo(iata="PHX", name="Phoenix Sky Harbor International Airport", city="Phoenix", state="AZ", latitude=33.435302, longitude=-112.005905, timezone="America/Phoenix"),
    "TUS": AirportInfo(iata="TUS", name="Tucson International Airport", city="Tucson", state="AZ", latitude=32.115004, longitude=-110.938053, timezone="America/Phoenix"),
    "LAS": AirportInfo(iata="LAS", name="Harry Reid International Airport", city="Las Vegas", state="NV", latitude=36.083361, longitude=-115.151817, timezone="America/Los_Angeles"),
    "ABQ": AirportInfo(iata="ABQ", name="Albuquerque International Sunport", city="Albuquerque", state="NM", latitude=35.039976, longitude=-106.608925, timezone="America/Denver"),
    "SEA": AirportInfo(iata="SEA", name="Seattle-Tacoma International Airport", city="Seattle", state="WA", latitude=47.447943, longitude=-122.310276, timezone="America/Los_Angeles"),
    "PDX": AirportInfo(iata="PDX", name="Portland International Airport", city="Portland", state="OR", latitude=45.588699, longitude=-122.598, timezone="America/Los_Angeles"),
    "SAN": AirportInfo(iata="SAN", name="San Diego International Airport", city="San Diego", state="CA", latitude=32.733601, longitude=-117.190002, timezone="America/Los_Angeles"),
    "SMF": AirportInfo(iata="SMF", name="Sacramento International Airport", city="Sacramento", state="CA", latitude=38.6954, longitude=-121.591003, timezone="America/Los_Angeles"),
    "SFO": AirportInfo(iata="SFO", name="San Francisco International Airport", city="San Francisco", state="CA", latitude=37.619806, longitude=-122.374821, timezone="America/Los_Angeles"),
    "OAK": AirportInfo(iata="OAK", name="Oakland San Francisco Bay Airport", city="Oakland", state="CA", latitude=37.720085, longitude=-122.221184, timezone="America/Los_Angeles"),
    "SJC": AirportInfo(iata="SJC", name="Mineta San Jose International Airport", city="San Jose", state="CA", latitude=37.362452, longitude=-121.929188, timezone="America/Los_Angeles"),
    "LAX": AirportInfo(iata="LAX", name="Los Angeles International Airport", city="Los Angeles", state="CA", latitude=33.942501, longitude=-118.407997, timezone="America/Los_Angeles"),
    "HNL": AirportInfo(iata="HNL", name="Daniel K. Inouye International Airport", city="Honolulu", state="HI", latitude=21.318387, longitude=-157.92567, timezone="Pacific/Honolulu"),
    "OGG": AirportInfo(iata="OGG", name="Kahului International Airport", city="Kahului", state="HI", latitude=20.896263, longitude=-156.431837, timezone="Pacific/Honolulu"),
    "ANC": AirportInfo(iata="ANC", name="Ted Stevens Anchorage International Airport", city="Anchorage", state="AK", latitude=61.179004, longitude=-149.992561, timezone="America/Anchorage"),
    "FAI": AirportInfo(iata="FAI", name="Fairbanks International Airport", city="Fairbanks", state="AK", latitude=64.815102, longitude=-147.856003, timezone="America/Anchorage"),
    "SJU": AirportInfo(iata="SJU", name="Luis Munoz Marin International Airport", city="San Juan", state="PR", latitude=18.4394, longitude=-66.001801, timezone="America/Puerto_Rico"),
}


# --------------------------------------------------------------------------- #
# Public lookup API
# --------------------------------------------------------------------------- #
def lookup_airport(iata: str) -> Optional[AirportInfo]:
    """Return the static record for ``iata``, or ``None`` when it is not covered.

    ``None`` means "this app has no audited coordinates for that code". The caller
    must turn that into an explicit error/unavailable state naming the gap; it must
    never substitute another airport, a city name, or a guessed point. Lookup is
    case-insensitive and tolerates surrounding whitespace.
    """
    if not isinstance(iata, str):
        return None
    return _AIRPORTS.get(iata.strip().upper())


def supported_airports() -> List[str]:
    """Every IATA code in the table, sorted - for validating input up front."""
    return sorted(_AIRPORTS)
