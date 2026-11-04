"""Tests for :mod:`ox_ui.core.simple_rpc_client`."""

import threading
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from ox_ui.core import simple_rpc_client
from ox_ui.core.simple_rpc_client import SimpleRPCCall, SingleTryTransport
from ox_ui.tkdantic import cli_builder

from .test_cli_builder import DropOnce


@pytest.fixture
def echo_url():
    """URL of a local XML-RPC server with an ``echo`` function."""
    server = SimpleXMLRPCServer(('127.0.0.1', 0), logRequests=False,
                                allow_none=True)
    server.register_function(lambda text: f'got {text}', 'echo')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f'http://{host}:{port}'
    server.shutdown()
    server.server_close()


def run_call(url, cmd_args, rpc_timeout=10, single_try=True):
    """Run a SimpleRPCCall of ``echo`` to the end; return it."""
    call = SimpleRPCCall(url=url, command_name='echo',
                         cmd_args=cmd_args, rpc_timeout=rpc_timeout,
                         single_try=single_try)
    call.start()
    call.join(10)
    return call


def test_call_returns_the_result(echo_url):
    """A JSON string argument is sent as it is."""
    assert run_call(echo_url, '{"a": 1}').result == 'got {"a": 1}'


def test_no_timeout_waits(echo_url):
    """rpc_timeout=None waits for the answer."""
    assert run_call(echo_url, 'x', rpc_timeout=None).result == 'got x'


def test_sent_once_when_the_connection_drops():
    """With single_try, a server that reads the request and hangs up
    gets it once."""
    server = DropOnce()
    try:
        call = run_call(server.url, 'x')
    finally:
        server.close()
    assert server.requests == 1
    assert 'Traceback' in call.result


def test_default_still_resends():
    """Without single_try the call goes through get_proxy, as before
    0.6.0: xmlrpc's Transport sends it again after the hang-up."""
    server = DropOnce()
    try:
        call = run_call(server.url, 'x', single_try=False)
    finally:
        server.close()
    assert server.requests == 2
    assert 'Traceback' in call.result


def test_default_call_returns_the_result(echo_url):
    """The default (get_proxy) path still works."""
    assert run_call(echo_url, 'y', single_try=False).result == 'got y'


def test_single_try_transport_still_in_cli_builder():
    """Code importing it from cli_builder (0.4.7) keeps working."""
    assert cli_builder.SingleTryTransport is SingleTryTransport
    assert simple_rpc_client.SingleTryTransport is SingleTryTransport
