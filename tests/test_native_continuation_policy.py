"""C47 continuation contracts through real native/fallback RPC production code."""
from copy import deepcopy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import swoop
from swoop import _adaptive, _native, _selection, rpc
from tests.factories import (decode_f_req, encode_rpc_outer, make_flight_segment,
                             make_full_response, make_itinerary_element)

MIA = 'socks5h://continuation-fixture:1082'


def itinerary(origin, via, destination, date, first_number):
    day = tuple(int(part) for part in date.split('-'))
    segments = [make_flight_segment(airline_code='AA',flight_number=first_number,
        dep_airport=origin,arr_airport=via,dep_date=day,arr_date=day),
        make_flight_segment(airline_code='AA',flight_number=str(int(first_number)+1),
        dep_airport=via,arr_airport=destination,dep_date=day,arr_date=day)]
    raw = make_itinerary_element(segments,airline_code='AA',summary_data=[[None,350],None])
    raw[0][3:9] = [origin,list(day),[8,30],destination,list(day),[11,45]]
    return raw


@pytest.fixture(autouse=True)
def clean():
    _adaptive._routes.clear()
    rpc._clients.clear()
    yield
    _adaptive._routes.clear()
    rpc._clients.clear()


@pytest.mark.parametrize('filters', [{}, {'exclude_connecting_airports':['MIA'],
    'airlines':['AA'],'max_stops':2,'max_duration':700,'earliest_departure':10,'latest_departure':24}])
def test_c47_prefix_narrowing_selector_and_pricing_survive_native_fallback(monkeypatch, filters):
    outbound = itinerary('JFK','MIA','PTY','2026-11-16','117')
    inbound = itinerary('PTY','ORD','JFK','2026-11-23','119')
    out_text = encode_rpc_outer(make_full_response(other_itins=[outbound]))
    in_text = encode_rpc_outer(make_full_response(other_itins=[inbound]))
    decoded = rpc._parse_rpc_response(out_text)
    assert decoded and decoded.other
    legs = [rpc._normalize_rpc_leg('JFK','PTY','2026-11-16'),
        rpc._normalize_rpc_leg('PTY','JFK','2026-11-23',airlines=['AA','UA'],
            connecting_airports=['MIA','ORD'],max_stops=1,max_duration=900,
            earliest_departure=6,latest_departure=22)]
    original = _selection.encode_trip_selector(request_legs=legs,itineraries=[decoded.other[0]],
        cabin='premium-economy',passengers=swoop.Passengers(adults=2,children=1),
        include_basic_economy=True,exclude_separate_tickets=True,
        carry_on_bags=1,checked_bags=2,max_price=900,transport=swoop.TransportConfig(country='PA',currency='EUR'))
    original_payload = _selection.decode_trip_selector(original)
    posts = []
    class HTTP:
        def __init__(self,family='auto',profile='chrome',proxy=None,**kwargs):
            self.route = proxy or family
        def post(self,url,*,content,headers,timeout):
            request = decode_f_req(parse_qs(content.decode())['f.req'][0])
            segments = request[1][13]
            posts.append((self.route,url,content,headers.copy(),deepcopy(request),timeout))
            if len(posts) == 1:
                return SimpleNamespace(status_code=403,text='',headers={})
            return SimpleNamespace(status_code=200,text=in_text if segments[0][8] else out_text,headers={})
    monkeypatch.setattr(_native,'NativeClient',HTTP)
    monkeypatch.setattr('primp.Client',HTTP)
    tc = swoop.TransportConfig(address_family='ipv6',fallback_proxies=(MIA,),timeout=20,
        request_timeout=8,country='PA',currency='EUR')
    result = swoop.search_next_leg(original,next_leg_filters=filters,transport=tc)
    assert len(result.results) == 1 and result.results[0].is_resolved
    assert [p[0] for p in posts] == ['ipv6',MIA,MIA]
    assert posts[0][1:4] == posts[1][1:4]  # exact prefix replay on fallback
    assert _selection.decode_trip_selector(original) == original_payload
    retained = _selection.decode_trip_selector(result.results[0].selector)
    assert retained['query_legs'][0] == original_payload['query_legs'][0]
    assert retained['selected_legs'][0] == original_payload['selected_legs'][0]
    assert retained['country'] == 'PA' and retained['currency'] == 'EUR'
    assert retained['passengers'] == swoop.Passengers(adults=2,children=1)
    assert retained['checked_bags'] == 2 and retained['max_price'] == 900
    assert posts[1][4][1][13][1] == rpc._build_segment_from_leg(legs[1])
    expected = dict(original_payload['query_legs'][1])
    if filters:
        expected.update(airlines=['AA'],connecting_airports=['ORD'],max_duration=700,earliest_departure=10)
    assert retained['query_legs'][1] == expected
    assert posts[2][4][1][13][1] == rpc._build_segment_from_leg(expected)
    assert posts[2][4][1][13][0][8] == retained['selected_legs'][0]
    price = swoop.price_selector(result.results[0].selector,transport=tc)
    assert price is not None and price.price == 350 and price.is_estimate
    assert all(p[0] == MIA and p[4][1][13][1] == rpc._build_segment_from_leg(expected) for p in posts[3:])
    assert all(p[5] <= 8 and 'gl=PA' in p[1] and 'EUR' in p[3]['x-goog-ext-259736195-jspb'] for p in posts)
    assert _adaptive.health()[0]['reason'] == 'http_403'


def test_c47_impossible_native_next_intersection_fails_before_any_dispatch(monkeypatch):
    raw = itinerary('JFK','MIA','PTY','2026-11-16','117')
    decoded = rpc._parse_rpc_response(encode_rpc_outer(make_full_response(other_itins=[raw])))
    assert decoded and decoded.other
    selector = _selection.encode_trip_selector(request_legs=[rpc._normalize_rpc_leg('JFK','PTY','2026-11-16'),
        rpc._normalize_rpc_leg('PTY','JFK','2026-11-23',airlines=['AA'])],itineraries=[decoded.other[0]],
        cabin='economy',include_basic_economy=True)
    monkeypatch.setattr(rpc,'_get_transport_client',lambda *_: pytest.fail('invalid narrowing dispatched'))
    with pytest.raises(ValueError,match='intersection'):
        swoop.search_next_leg(selector,next_leg_filters={'airlines':['UA']},
            transport=swoop.TransportConfig(address_family='ipv6',fallback_proxies=(MIA,)))
    assert _adaptive.health() == []
