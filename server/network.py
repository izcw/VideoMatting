"""发现本机可用于局域网访问的 IPv4 地址。"""
import ipaddress
import socket

import psutil


def lan_addresses():
    addresses = set()
    states = psutil.net_if_stats()
    for name, entries in psutil.net_if_addrs().items():
        if name in states and not states[name].isup:
            continue
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            try:
                address = ipaddress.IPv4Address(entry.address)
            except ipaddress.AddressValueError:
                continue
            if address.is_private and not (address.is_loopback or address.is_link_local or address.is_unspecified):
                addresses.add(str(address))
    result = sorted(addresses, key=ipaddress.IPv4Address)
    # UDP connect 只查询默认路由，不发送数据，也不要求联网。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(('192.0.2.1', 80))
            primary = connection.getsockname()[0]
        if primary in result:
            result.remove(primary)
            result.insert(0, primary)
    except OSError:
        pass
    return result


def allowed_hosts():
    return ['127.0.0.1', 'localhost', 'testserver', *lan_addresses()]
