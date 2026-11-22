"""Resolve the Ubuntu MCP listener on physical LAN interfaces at launch."""
from ipaddress import IPv4Address
import json
from pathlib import Path
import subprocess

from ..settings import private_lan


_NET_INTERFACES = Path('/sys/class/net')


def mcp_bind_host(host):
    if host != 'lan':
        return host
    try:
        links = json.loads(subprocess.check_output(
            ['ip', '-j', '-4', 'address', 'show', 'up', 'scope', 'global'],
            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3))
        addresses = []
        for link in links:
            if 'LOWER_UP' not in link['flags'] or not (_NET_INTERFACES/link['ifname']/'device').exists():
                continue
            for entry in link['addr_info']:
                if entry['family'] == 'inet' and entry['scope'] == 'global':
                    address = IPv4Address(entry['local'])
                    if private_lan(address):
                        addresses.append(str(address))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('Unable to read LAN addresses. Set mcp.host to the trusted LAN IPv4 address') from exc
    if len(addresses) != 1:
        raise ValueError('Expected one private IPv4 on an active hardware interface. Set mcp.host explicitly')
    return addresses[0]
