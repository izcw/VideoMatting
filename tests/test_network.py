import socket
from types import SimpleNamespace

from server import network


def test_lan_addresses_choose_route_and_exclude_unusable_interfaces(monkeypatch):
    def address(value):
        return SimpleNamespace(family=socket.AF_INET, address=value)
    monkeypatch.setattr(network.psutil, 'net_if_stats', lambda: {
        'LAN': SimpleNamespace(isup=True), 'offline': SimpleNamespace(isup=False)})
    monkeypatch.setattr(network.psutil, 'net_if_addrs', lambda: {
        'LAN': [address('192.168.1.8'), address('10.0.0.5'), address('127.0.0.1'),
                address('169.254.1.1'), address('0.0.0.0'), address('8.8.8.8')],
        'offline': [address('192.168.2.8')]})
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect(self, target): pass
        def getsockname(self): return ('192.168.1.8', 1234)
    monkeypatch.setattr(network.socket, 'socket', lambda *args: Connection())
    assert network.lan_addresses() == ['192.168.1.8', '10.0.0.5']
    assert '192.168.1.8' in network.allowed_hosts()


def test_no_network_still_allows_loopback(monkeypatch):
    monkeypatch.setattr(network.psutil, 'net_if_stats', lambda: {})
    monkeypatch.setattr(network.psutil, 'net_if_addrs', lambda: {})
    def unavailable(*args):
        raise OSError('No route')
    monkeypatch.setattr(network.socket, 'socket', unavailable)
    assert network.lan_addresses() == []
    assert network.allowed_hosts() == ['127.0.0.1', 'localhost', 'testserver']
