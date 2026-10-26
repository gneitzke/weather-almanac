"""wifi-announce: the Pi announces its own address, because the mesh passes no
broadcasts to wireless clients and its record of a Pi goes stale after a roam
(2026-10-26). The frames must be exactly what other stacks learn from, and a
failure must be reported, never raised into the keepalive."""
import importlib.util
import socket
import struct
from pathlib import Path

KIOSK = Path('design/almanac/kiosk')
spec = importlib.util.spec_from_file_location('wifi_announce', KIOSK / 'wifi-announce.py')
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)

MAC = bytes.fromhex('020000000001')        # locally administered, not a real device
IP = socket.inet_aton('192.0.2.10')        # TEST-NET-1: no real LAN address in a public repo


def parse(frame):
    dst, src, etype = frame[0:6], frame[6:12], struct.unpack('!H', frame[12:14])[0]
    htype, ptype, hlen, plen, op, sha, spa, tha, tpa = struct.unpack('!HHBBH6s4s6s4s', frame[14:42])
    return dict(dst=dst, src=src, etype=etype, htype=htype, ptype=ptype, hlen=hlen, plen=plen, op=op,
                sha=sha, spa=spa, tha=tha, tpa=tpa, length=len(frame))


def test_an_announcement_and_a_gratuitous_reply_both_broadcast_our_own_address():
    request, reply = (parse(f) for f in wa.frames(MAC, IP))
    for f in (request, reply):
        assert f['dst'] == b'\xff' * 6 and f['src'] == MAC and f['etype'] == 0x0806
        assert (f['htype'], f['ptype'], f['hlen'], f['plen']) == (1, 0x0800, 6, 4)
        assert f['sha'] == MAC and f['spa'] == IP and f['tpa'] == IP     # sender = target: an announcement
        assert f['length'] == 60                                           # the Ethernet minimum, padded
    assert request['op'] == 1 and request['tha'] == b'\x00' * 6           # RFC 5227 announcement
    assert reply['op'] == 2 and reply['tha'] == b'\xff' * 6               # gratuitous reply


class FakeSocket:
    def __init__(self):
        self.bound, self.sent = None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def bind(self, address):
        self.bound = address

    def getsockname(self):
        return ('wlan0', 0x0003, 0, 1, MAC + b'\x00\x00')                 # AF_PACKET pads the address

    def send(self, frame):
        self.sent.append(frame)


def test_announce_sends_each_round_and_reports_what_it_said():
    sock, sleeps = FakeSocket(), []
    ip, mac = wa.announce('wlan0', count=3, interval=1.0, open_socket=lambda: sock,
                          ipv4=lambda iface: IP, sleep=sleeps.append)
    assert (ip, mac) == ('192.0.2.10', '02:00:00:00:00:01')
    assert sock.bound == ('wlan0', 0)
    assert len(sock.sent) == 6 and sleeps == [1.0, 1.0]                   # three rounds, a pause between
    assert all(parse(f)['sha'] == MAC for f in sock.sent)


def test_no_address_or_no_socket_is_reported_not_raised(monkeypatch, capsys):
    def no_address(iface):
        raise OSError(99, 'Cannot assign requested address')
    monkeypatch.setattr(wa, 'iface_ipv4', no_address)                    # Wi-Fi down: no IPv4 on wlan0
    assert wa.main(['wifi-announce', 'wlan0']) == 1
    assert capsys.readouterr().out.strip() == 'announce on wlan0 failed: Cannot assign requested address'

    def no_raw_sockets():
        raise PermissionError(1, 'Operation not permitted')
    monkeypatch.setattr(wa, 'iface_ipv4', lambda iface: IP)
    monkeypatch.setattr(wa, 'open_packet_socket', no_raw_sockets)        # run without root
    assert wa.main(['wifi-announce', 'wlan0']) == 1
    assert capsys.readouterr().out.strip() == 'announce on wlan0 failed: Operation not permitted'


def test_usage():
    assert wa.main(['wifi-announce']) == 2
    assert wa.main(['wifi-announce', 'wlan0', 'many']) == 2


def test_the_keepalive_announces_every_check_and_keeps_one_line_a_minute():
    sh = (KIOSK / 'wifi-keepalive.sh').read_text()
    assert 'ANNOUNCE="${WIFI_ANNOUNCE:-/usr/local/sbin/wifi-announce}"' in sh
    body = sh[sh.index('exec 9>/run/wifi-keepalive.lock'):]
    # after the lock (serialized with recovery), before the nudge and the probe
    assert body.index('"$ANNOUNCE" "$IFACE"') < body.index('bridge nudge to last LAN viewer') < body.index('if probe "$PEER"')
    assert 'log "announce failed: ${out:-no output}"' in sh                 # a failure gets its own line
    assert '; $announced; no recovery needed"' in sh                       # success rides on the peer line
    setup = (KIOSK / 'PI4-SETUP.md').read_text()
    assert 'sudo install -m 0755 wifi-announce.py /usr/local/sbin/wifi-announce' in setup
