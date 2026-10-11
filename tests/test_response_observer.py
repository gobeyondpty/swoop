"""Raw bodies survive decoding failures without changing queries or diagnostics."""
from dataclasses import asdict, fields
from types import SimpleNamespace

import pytest

import swoop
from swoop import rpc
from swoop._deals import _establish_session
from swoop._explore import _get_browser_params
from swoop._hotels import _post_travel_rpc
from swoop.exceptions import SwoopHTTPError, SwoopParseError, SwoopRateLimitError, SwoopTransportError
from tests.factories import encode_rpc_outer, make_full_response
from tests.test_search_page import rejected_exit


class Response:
    def __init__(self, status, body):
        self.status_code, self.body, self.reads = status, body, 0

    @property
    def text(self):
        self.reads += 1
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Client:
    def __init__(self, *responses):
        self.responses, self.posts, self.gets = iter(responses), 0, 0

    def post(self, *_args, **_kwargs):
        self.posts += 1
        return next(self.responses)

    def get(self, *_args, **_kwargs):
        self.gets += 1
        return next(self.responses)


def test_success_captures_once_before_parse_without_body_in_diagnostics(monkeypatch):
    events = []
    body = encode_rpc_outer(make_full_response())
    response = Response(200, body)
    client = Client(response)
    monkeypatch.setattr(rpc, '_get_client', lambda *_: client)
    original = rpc._parse_rpc_response
    def parse(text):
        assert events[0].body == text == body
        return original(text)
    monkeypatch.setattr(rpc, '_parse_rpc_response', parse)
    diagnostics = swoop.DiagnosticsRecorder()
    result = swoop.search_raw('JFK', 'LAX', '2026-11-15', transport=swoop.TransportConfig(
        retries=0, diagnostics=diagnostics, response_observer=events.append))
    assert result is not None and client.posts == 1 and response.reads == 1
    assert events[0].operation == 'shopping'
    assert events[0].transport == 'rpc'
    assert body not in repr(events[0]) and 'body=' not in repr(events[0])
    assert body not in str(asdict(diagnostics.snapshot()))
    assert {f.name for f in fields(swoop.ResponseBody)} == {
        'operation', 'transport', 'method', 'retry_index', 'status_code', 'body', 'body_unavailable'}


@pytest.mark.parametrize('status,error', [(200,SwoopParseError),(503,SwoopHTTPError),(429,SwoopRateLimitError)])
def test_raw_non_json_failure_is_captured_before_typed_error(monkeypatch, status, error):
    response = Response(status, '<html>unknown Google response fields</html>')
    client, events = Client(response), []
    monkeypatch.setattr(rpc, '_get_client', lambda *_: client)
    with pytest.raises(error):
        swoop.search_raw('JFK','LAX','2026-11-15', transport=swoop.TransportConfig(
            retries=0,response_observer=events.append))
    assert events[0].body == response.body and events[0].status_code == status
    assert client.posts == response.reads == 1


def test_rejected_body_is_not_read_without_opt_in():
    response = Response(503, AssertionError('previous rejection behavior must be preserved'))
    with pytest.raises(SwoopHTTPError):
        rpc._post_with_retry(Client(response),rpc.BOOKING_RPC_URL,b'',{},transport=swoop.TransportConfig(retries=0))
    assert response.reads == 0


@pytest.mark.parametrize('status,error',[(200,SwoopTransportError),(429,SwoopRateLimitError),(503,SwoopHTTPError)])
def test_unreadable_body_does_not_replace_authoritative_failure(status, error):
    response, events = Response(status, TimeoutError('private read message')), []
    with pytest.raises(error):
        rpc._post_with_retry(Client(response),rpc.BOOKING_RPC_URL,b'',{},transport=swoop.TransportConfig(
            retries=0,response_observer=events.append))
    assert len(events) == 1 and events[0].body is None and events[0].body_unavailable


def test_observer_failure_cannot_change_success_or_http_rejection():
    def reject(_event):
        raise RuntimeError('private storage error')
    response = rpc._post_with_retry(Client(Response(200,'ok')),rpc.BOOKING_RPC_URL,b'',{},
        transport=swoop.TransportConfig(response_observer=reject))
    assert response.text == 'ok'
    with pytest.raises(SwoopHTTPError):
        rpc._post_with_retry(Client(Response(503,'upstream rejection')),rpc.BOOKING_RPC_URL,b'',{},
            transport=swoop.TransportConfig(response_observer=reject))


def test_rpc_rejection_and_page_success_both_preserved(rejected_exit):
    events = []
    result = swoop.search_raw('JFK','LAX','2026-11-15', transport=swoop.TransportConfig(
        retries=0,response_observer=events.append))
    assert result.best or result.other
    assert [event.transport for event in events] == ['rpc','page']
    assert events[1].body == rejected_exit.html
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1


@pytest.mark.parametrize('status,error',[(200,SwoopParseError),(503,SwoopHTTPError),(429,SwoopRateLimitError)])
def test_hotels_rpc_unknown_fields_and_non_json_rejections_are_observed(status,error):
    response, events = Response(status,'not a batchexecute response'), []
    client = Client(response)
    with pytest.raises(error):
        _post_travel_rpc(client,'AtySUc',[],page_url='https://example.test',
            transport=swoop.TransportConfig(retries=0,response_observer=events.append))
    assert len(events)==1 and events[0].operation=='hotels' and events[0].body==response.body
    assert client.posts==response.reads==1


def test_deals_session_page_capture_is_optional_and_read_failure_cannot_change_session():
    events=[]
    response=Response(200,TimeoutError('ignored page body'))
    client=Client(response)
    _establish_session(client,transport=swoop.TransportConfig(response_observer=events.append))
    assert client.gets==response.reads==1 and events[0].body_unavailable
    assert events[0].operation=='deals' and events[0].transport=='page'
    ignored=Response(200,'not read')
    _establish_session(Client(ignored))
    assert ignored.reads==0


def test_explore_bootstrap_is_observed_without_duplicate_page_read():
    events=[]
    response=Response(200,'unknown explore bootstrap fields')
    client=Client(response)
    _get_browser_params(client,'https://example.test',key='raw-observer-fixture',force_refresh=True,
        transport=swoop.TransportConfig(response_observer=events.append))
    assert response.reads==client.gets==1 and events[0].body==response.body
    assert events[0].operation=='explore'
