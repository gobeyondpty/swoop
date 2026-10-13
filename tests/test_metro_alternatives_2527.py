"""ndcterm #2527: a metro endpoint is one native shopping pass with alternative airports.

``shopping_alternatives_lhr_ewr.json`` is a live GetShoppingResults answer captured
2026-10-13 for LHR/LGW/STN/LTN/LCY/SEN -> EWR on 2026-10-22, EWR -> same list on
2026-10-27 (180 rows), trimmed to the first row per departure airport.
"""
import base64
import json
from pathlib import Path

import pytest

import swoop
from swoop import SearchLeg
from swoop._selection import SELECTOR_PREFIX, decode_trip_selector

LON = ["LHR", "LGW", "STN", "LTN", "LCY", "SEN"]
FIXTURE = Path(__file__).parent / "fixtures/responses/shopping_alternatives_lhr_ewr.json"


def _rpc_text(payload: list) -> str:
    return ")]}'\n\n" + json.dumps([["wrb.fr", None, json.dumps(payload)]])


def _lon_ewr_roundtrip() -> list[SearchLeg]:
    return [
        SearchLeg(date="2026-10-22", from_airport=LON, to_airport="EWR"),
        SearchLeg(date="2026-10-27", from_airport="EWR", to_airport=LON),
    ]


def test_search_legs_sends_alternatives_in_one_pass(monkeypatch):
    captured = {}

    def fake_search_trip_options(legs, **kwargs):
        captured["legs"] = legs
        return swoop.SearchResult()

    monkeypatch.setattr(swoop, "search_trip_options", fake_search_trip_options)

    swoop.search_legs([SearchLeg(date="2026-10-22", from_airport=["lhr", "LGW"], to_airport="ewr")])

    assert captured["legs"][0]["origin"] == ["LHR", "LGW"]
    assert captured["legs"][0]["destination"] == "EWR"


def test_single_airport_leg_is_unchanged(monkeypatch):
    captured = {}
    monkeypatch.setattr(swoop, "search_trip_options", lambda legs, **kw: captured.setdefault("legs", legs) and swoop.SearchResult())

    swoop.search_legs([SearchLeg(date="2026-10-22", from_airport="lhr", to_airport="ewr")])

    assert captured["legs"][0]["origin"] == "LHR"
    assert captured["legs"][0]["destination"] == "EWR"


@pytest.mark.parametrize("alternatives", [["LHR", "LON"], [], ["LHR", "LHR"],
    ["LHR", "LGW", "STN", "LTN", "LCY", "SEN", "CDG", "ORY", "BVA", "AMS", "FRA"]])
def test_alternatives_are_validated_per_member(alternatives):
    with pytest.raises(ValueError, match=r"legs\[0\]\.from_airport"):
        swoop.search_legs([SearchLeg(date="2026-10-22", from_airport=alternatives, to_airport="EWR")])


def test_alternatives_resolve_each_result_to_its_real_airport(fake_primp):
    fake_primp(200, _rpc_text(json.loads(FIXTURE.read_text())))

    result = swoop.search_legs(_lon_ewr_roundtrip(), expand_legs=False)

    origins = {trip.legs[0].origin for trip in result.results}
    assert origins == {"LHR", "LGW", "STN", "LCY"}
    for trip in result.results:
        leg = trip.legs[0]
        assert leg.itinerary is not None
        assert leg.origin == leg.itinerary.segments[0].departure_airport_code
        assert leg.destination == "EWR"
        assert decode_trip_selector(trip.selector)["query_legs"][0]["origin"] == LON


def test_selector_with_alternatives_continues_from_a_member_airport(fake_primp):
    fake_primp(200, _rpc_text(json.loads(FIXTURE.read_text())))
    selected = swoop.search_legs(_lon_ewr_roundtrip(), expand_legs=False).results[0]

    continued = swoop.search_next_leg(selected.selector)

    assert continued.results
    assert all(trip.legs[0].origin == selected.legs[0].origin for trip in continued.results)


def test_selector_rejects_a_selected_flight_outside_the_alternatives(fake_primp):
    fake_primp(200, _rpc_text(json.loads(FIXTURE.read_text())))
    selected = swoop.search_legs(_lon_ewr_roundtrip(), expand_legs=False).results[0]
    encoded = selected.selector[len(SELECTOR_PREFIX):]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["selected_legs"][0][0][0] = "MAN"
    forged = SELECTOR_PREFIX + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(ValueError, match="selected segments do not match requested bounds"):
        swoop.search_next_leg(forged)
