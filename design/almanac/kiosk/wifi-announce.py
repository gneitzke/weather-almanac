#!/usr/bin/env python3
"""Announce this Pi's IPv4 address on its Wi-Fi interface (RFC 5227 ARP).

The home mesh delivers no broadcast or multicast frames to its wireless
clients: on 2026-10-26 both Pis received none in 30 s (no ARP, no mDNS) while
their unicast traffic flowed. So another host can find a Pi only through the
mesh's own record of where that Pi is, and that record goes stale after a roam
or simply ages out. The Pi then drops off the LAN while its outbound traffic
still works (the Pi 3 roamed between nodes at 03:50 and nothing on the LAN,
not even the Pi 4 on the same node, reached it for nine hours). A broadcast
from the Pi crosses every node and the wired side, re-teaching each bridge
table, the mesh's ARP proxy and every host that already knows the Pi.
wifi-keepalive.sh runs this on every check (each minute).

Usage (root): wifi-announce <iface> [count]
Prints `announced <ip> at <mac> on <iface>`; exits 1 with the reason when the
interface has no IPv4 address or the frames cannot be sent.
"""
import fcntl
import socket
import struct
import sys
import time

SIOCGIFADDR = 0x8915
AF_PACKET = getattr(socket, 'AF_PACKET', 17)   # Linux only; tests inject a socket


def frames(mac, ip):
    """Both broadcast: the announcement (an ARP request for our own address,
    target hardware address zero) and a gratuitous reply. Some stacks update
    their caches from one, some from the other."""
    out = []
    for op, tha in ((1, b'\x00' * 6), (2, b'\xff' * 6)):
        arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x0800, 6, 4, op, mac, ip, tha, ip)
        out.append(b'\xff' * 6 + mac + b'\x08\x06' + arp + b'\x00' * 18)   # padded to 60 bytes
    return out


def iface_ipv4(iface):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        return fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack('256s', iface[:15].encode()))[20:24]


def open_packet_socket():
    return socket.socket(AF_PACKET, socket.SOCK_RAW)


def announce(iface, count=2, interval=1.0, open_socket=None, ipv4=None, sleep=time.sleep):
    """Send `count` rounds, `interval` apart (a lost frame is likely on Wi-Fi).
    Returns (ip, mac) as text."""
    ip = (ipv4 or iface_ipv4)(iface)
    with (open_socket or open_packet_socket)() as s:
        s.bind((iface, 0))
        mac = s.getsockname()[4][:6]
        for i in range(count):
            for frame in frames(mac, ip):
                s.send(frame)
            if i + 1 < count:
                sleep(interval)
    return socket.inet_ntoa(ip), mac.hex(':')


def main(argv):
    if len(argv) not in (2, 3) or (len(argv) == 3 and not argv[2].isdigit()):
        print('usage: wifi-announce <iface> [count]', file=sys.stderr)
        return 2
    iface, count = argv[1], int(argv[2]) if len(argv) == 3 else 2
    try:
        ip, mac = announce(iface, max(1, min(count, 5)))
    except OSError as error:
        print(f'announce on {iface} failed: {error.strerror or error}')
        return 1
    print(f'announced {ip} at {mac} on {iface}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
