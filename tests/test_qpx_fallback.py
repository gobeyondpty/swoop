"""The fork's page fallback must retain QPX pricing inputs."""
import base64
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

from swoop import flights_pb2 as PB
from swoop._search_page import fetch_search_page
from swoop.models import Passengers,TransportConfig
from swoop.rpc import _normalize_rpc_leg


def test_fallback_tfs_and_currency():
    calls=[]
    client=SimpleNamespace(get=lambda url,**kwargs: (calls.append(url) or SimpleNamespace(status_code=200,text="AF_initDataCallback({key:'ds:1',data:[null,null,[[]],[[]]]});")))
    fetch_search_page(client,[_normalize_rpc_leg('JFK','LAX','2026-12-03',max_duration=700,connecting_airports=['ATL'],min_layover_duration=30,max_layover_duration=120)],cabin='economy',passengers=Passengers(),sort=1,exclude_basic_economy=True,carry_on_bags=1,checked_bags=1,max_price=500,transport=TransportConfig(country='GB',currency='JPY'))
    query=parse_qs(urlsplit(calls[0]).query)
    assert query['gl']==['GB'] and query['curr']==['JPY']
    info=PB.Info();info.ParseFromString(base64.b64decode(query['tfs'][0]))
    assert info.baggage.carry_on_bags==info.baggage.checked_bags==1
    assert info.max_price==500 and info.data[0].max_duration==700
    assert list(info.data[0].connecting_airports)==['ATL']
    assert (info.data[0].min_layover_duration,info.data[0].max_layover_duration)==(30,120)
