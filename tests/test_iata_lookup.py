"""Tests for the static airport table in ``data_sources.iata_lookup``.

No network and no fabricated rows: these tests pin the invariants the table must
hold - coverage of well-known airports, correct coordinates, no silently duplicated
keys, complete records, US bounds - plus the failure mode that matters most: an
unknown code returns ``None`` so the caller *has* to report the gap explicitly. The
live cross-check of the table against NWS itself lives in ``test_nws_live.py``.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from data_sources import iata_lookup

# (iata, served city, state, latitude, longitude). The coordinates come from the
# OurAirports dataset and were verified against the live NWS /points endpoint; these
# rows are pinned exactly so a swapped or edited column cannot slip through.
KNOWN_ROWS: Tuple[Tuple[str, str, str, float, float], ...] = (
    ("JFK", "New York", "NY", 40.639447, -73.779317),
    ("SFO", "San Francisco", "CA", 37.619806, -122.374821),
    ("LAX", "Los Angeles", "CA", 33.942501, -118.407997),
    ("DEN", "Denver", "CO", 39.860027, -104.673792),
    ("MIA", "Miami", "FL", 25.796011, -80.289751),
    ("ANC", "Anchorage", "AK", 61.179004, -149.992561),
)

# Codes a demo user can plausibly type that this table deliberately does not cover.
UNSUPPORTED_CODES: Tuple[str, ...] = (
    "LHR",
    "CDG",
    "NRT",
    "XYZ",
    "JF",
    "JFKX",
    "",
    "   ",
    "123",
)

# Inclusive bounds for every row: CONUS, Alaska, Hawaii and Puerto Rico.
MIN_LATITUDE: float = 18.0
MAX_LATITUDE: float = 72.0
MIN_LONGITUDE: float = -180.0
MAX_LONGITUDE: float = -65.0

_IANA_TIMEZONE = re.compile(r"^[A-Za-z]+(?:/[A-Za-z_]+)+$")


def _all_airports() -> List[iata_lookup.AirportInfo]:
    """Every record in the table, reached only through the public API."""
    return [iata_lookup.lookup_airport(code) for code in iata_lookup.supported_airports()]


def _table_literal_keys() -> List[str]:
    """The keys as written in the ``_AIRPORTS`` dict literal in the source.

    ``len(_AIRPORTS)`` cannot detect a duplicated key (Python keeps the last one
    silently), so the literal is parsed and its keys compared instead.
    """
    source = Path(iata_lookup.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [
                target.id for target in node.targets if isinstance(target, ast.Name)
            ]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        else:
            continue
        if "_AIRPORTS" in names and isinstance(value, ast.Dict):
            return [
                key.value
                for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
    raise AssertionError("_AIRPORTS dict literal was not found in iata_lookup.py")


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def test_supported_airports_are_sorted_uppercase_three_letter_codes() -> None:
    """The list used to validate user input is clean and deterministic."""
    codes = iata_lookup.supported_airports()

    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    assert all(len(code) == 3 and code.isupper() and code.isalpha() for code in codes)
    assert len(codes) >= 40  # the demo coverage the table promises


@pytest.mark.parametrize("iata, city, state, latitude, longitude", KNOWN_ROWS)
def test_known_codes_resolve_to_the_expected_record(
    iata: str, city: str, state: str, latitude: float, longitude: float
) -> None:
    """Well-known codes resolve, with the exact coordinates the source published."""
    info = iata_lookup.lookup_airport(iata)

    assert info is not None
    assert (info.iata, info.city, info.state) == (iata, city, state)
    assert (info.latitude, info.longitude) == (latitude, longitude)
    assert info.name  # a real name, not a placeholder
    assert info.timezone


def test_lookup_is_case_insensitive_and_whitespace_tolerant() -> None:
    """A UI that passes " jfk " gets the same record as "JFK"."""
    expected = iata_lookup.lookup_airport("JFK")

    assert iata_lookup.lookup_airport("jfk") == expected
    assert iata_lookup.lookup_airport("  JfK  ") == expected


@pytest.mark.parametrize("iata", UNSUPPORTED_CODES)
def test_unsupported_codes_return_none(iata: str) -> None:
    """No guessing: an unknown code is a None the caller must report."""
    assert iata_lookup.lookup_airport(iata) is None


@pytest.mark.parametrize("value", [None, 42, ["JFK"], {"iata": "JFK"}])
def test_non_string_input_returns_none_instead_of_raising(value: Any) -> None:
    """Wrong-typed input degrades to the same explicit None, never a TypeError."""
    assert iata_lookup.lookup_airport(value) is None


# --------------------------------------------------------------------------- #
# Table invariants
# --------------------------------------------------------------------------- #
def test_table_has_no_duplicated_keys_in_the_source_literal() -> None:
    """A duplicated IATA key would silently drop a row: catch it in the literal."""
    keys = _table_literal_keys()

    duplicated = sorted(key for key in set(keys) if keys.count(key) > 1)
    assert duplicated == [], "duplicate IATA keys: {0}".format(duplicated)
    assert sorted(keys) == iata_lookup.supported_airports()


def test_every_record_is_complete_and_consistent() -> None:
    """Each row has the required fields, and its dict key matches the record."""
    for code in iata_lookup.supported_airports():
        info = iata_lookup.lookup_airport(code)

        assert isinstance(info, iata_lookup.AirportInfo)
        assert info.iata == code  # catches a copy/pasted row keyed to the wrong code
        assert isinstance(info.name, str) and len(info.name.strip()) > 3
        assert isinstance(info.city, str) and info.city.strip()
        assert re.match(r"^[A-Z]{2}$", info.state), info.state
        assert isinstance(info.latitude, float)
        assert isinstance(info.longitude, float)
        assert _IANA_TIMEZONE.match(info.timezone), info.timezone


def test_coordinates_are_inside_the_expected_us_bounds() -> None:
    """Coordinates sit inside CONUS/Alaska/Hawaii/Puerto Rico, never swapped."""
    for info in _all_airports():
        assert MIN_LATITUDE <= info.latitude <= MAX_LATITUDE, info
        assert MIN_LONGITUDE <= info.longitude <= MAX_LONGITUDE, info


def test_coordinates_property_is_six_decimal_lat_lon() -> None:
    """The NWS-ready coordinate string is exact and unambiguous."""
    assert iata_lookup.lookup_airport("JFK").coordinates == "40.639447,-73.779317"
    assert iata_lookup.lookup_airport("SFO").coordinates == "37.619806,-122.374821"

    for info in _all_airports():
        latitude, _, longitude = info.coordinates.partition(",")
        assert len(latitude.split(".")[1]) == 6
        assert len(longitude.split(".")[1]) == 6


def test_supported_airports_returns_a_fresh_list() -> None:
    """Callers cannot mutate the table through the returned list."""
    first = iata_lookup.supported_airports()
    first.append("ZZZ")

    assert "ZZZ" not in iata_lookup.supported_airports()
    assert iata_lookup.lookup_airport("ZZZ") is None


# --------------------------------------------------------------------------- #
# Record validation
# --------------------------------------------------------------------------- #
def _valid_record() -> dict:
    """A complete record used as the baseline for validation cases."""
    return {
        "iata": "JFK",
        "name": "John F. Kennedy International Airport",
        "city": "New York",
        "state": "NY",
        "latitude": 40.639447,
        "longitude": -73.779317,
        "timezone": "America/New_York",
    }


def test_record_normalises_the_iata_code() -> None:
    """Construction normalises case and whitespace so lookups compare cleanly."""
    info = iata_lookup.AirportInfo(**{**_valid_record(), "iata": " jfk "})

    assert info.iata == "JFK"
    assert info.to_dict()["iata"] == "JFK"


@pytest.mark.parametrize(
    "override",
    [
        {"iata": "JF"},
        {"iata": "12X"},
        {"iata": ""},
        {"latitude": 999.0},
        {"latitude": -91.0},
        {"longitude": -999.0},
        {"longitude": 200.0},
        {"latitude": "40.6"},
        {"longitude": None},
        {"latitude": True},
        {"name": ""},
        {"city": "  "},
        {"state": ""},
        {"timezone": ""},
    ],
)
def test_record_rejects_bad_rows_at_construction(override: dict) -> None:
    """A typo in the table fails loudly instead of serving a wrong point."""
    with pytest.raises(ValueError):
        iata_lookup.AirportInfo(**{**_valid_record(), **override})


def test_records_are_frozen() -> None:
    """Table rows are read-only reference data."""
    info = iata_lookup.lookup_airport("JFK")

    with pytest.raises(dataclasses.FrozenInstanceError):
        info.latitude = 0.0  # type: ignore[misc]
