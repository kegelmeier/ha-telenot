#!/usr/bin/env python3
"""Decode a packet capture of the BuildSec app talking to a Telenot panel.

Usage:
    python tools/decode_capture.py capture.pcap [--port N] [--show-code]

Secrets are never taken from the command line:
    TELENOT_FREISCHALTCODE  operator access (16 hex chars), prompted if unset
    TELENOT_AES_KEY         installer / remote control access (32 hex chars)

Without a key, only the plaintext handshake and envelope sizes are shown.
The keypad code inside the session-start block is masked unless --show-code.
"""

from __future__ import annotations

import argparse
import getpass
import os
import struct
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "telenot"))

from pytelenot.buildsec import Key, lcd_decode, split_records  # noqa: E402
from pytelenot.crypto import KEY_NUMBER_BETREIBER, KEY_NUMBER_PLAIN, derive_betreiber_key, unseal  # noqa: E402
from pytelenot.s2 import S2Telegram  # noqa: E402

IK_NAMES = {1: "SYNC_REQ", 2: "SYNC_RESP", 3: "POLL", 4: "DATA", 5: "ERR_IK", 6: "ERR_PK", 7: "SERVICE_REQ"}
NK_NAMES = {1: "", 5: "POLLTIME_SET", 6: "POLLTIME_ACK", 9: "SESSION_CLOSE", 0xFC: "CANCELLED", 0xFD: "VERSION", 0xFE: "INVALID_ID"}
KEY_NAMES = {k.value: k.name for k in Key}
ST70_NAMES = {
    0x92: "START operator", 0x94: "START installer", 0x9E: "START remote control",
    0x8A: "START legacy operator", 0x8C: "START legacy installer",
    0x93: "STARTED operator", 0x95: "STARTED installer", 0x9F: "STARTED remote control",
    0x8B: "STARTED legacy operator", 0x8D: "STARTED legacy installer",
    0x18: "GTYP request", 0x19: "GTYP", 0x23: "ERROR", 0x4B: "FERNVERB_OK", 0x8E: "END (iOS)",
    0x98: "LABEL request", 0x99: "COM2BUS",
}


# ---------------------------------------------------------------- pcap reading

def read_packets(path: Path):
    """Yield (timestamp, linktype, frame) from pcap or pcapng."""
    data = path.read_bytes()
    magic = data[:4]
    if data[24:28] == b"\x0a\x0d\x0d\x0a":  # macOS tcpdump on rvi: pcap header + pcapng
        data, magic = data[24:], data[24:28]
    if magic == b"\x0a\x0d\x0d\x0a":
        yield from _read_pcapng(data)
        return
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        end = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        end = ">"
    else:
        raise SystemExit("Not a pcap/pcapng file")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack_from(end + "I", data, 20)[0]
    off = 24
    while off + 16 <= len(data):
        sec, frac, incl, _orig = struct.unpack_from(end + "IIII", data, off)
        off += 16
        yield sec + frac / (1e9 if nano else 1e6), linktype, data[off : off + incl]
        off += incl


def _read_pcapng(data: bytes):
    end = "<"
    links: list[tuple[int, float]] = []
    off = 0
    while off + 12 <= len(data):
        btype = struct.unpack_from(end + "I", data, off)[0]
        if btype == 0x0A0D0D0A:
            end = "<" if data[off + 8 : off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            links = []
        blen = struct.unpack_from(end + "I", data, off + 4)[0]
        body = data[off + 8 : off + blen - 4]
        if btype == 1:
            links.append((struct.unpack_from(end + "H", body, 0)[0], 1e-6))
        elif btype == 6:
            iface, hi, lo, cap = struct.unpack_from(end + "IIII", body, 0)
            lt, res = links[iface] if iface < len(links) else (1, 1e-6)
            yield ((hi << 32) | lo) * res, lt, body[20 : 20 + cap]
        if blen < 12:
            break
        off += blen


def ip_payload(linktype: int, frame: bytes) -> bytes | None:
    if linktype == 258:  # PKTAP: header length, then inner DLT
        hlen, dlt = struct.unpack_from("<II", frame, 0)
        return ip_payload(dlt, frame[hlen:])
    if linktype == 1:  # Ethernet
        etype = struct.unpack_from(">H", frame, 12)[0]
        off = 14
        if etype == 0x8100:
            etype, off = struct.unpack_from(">H", frame, 16)[0], 18
        return frame[off:] if etype in (0x0800, 0x86DD) else None
    if linktype in (0, 108):  # BSD loopback / null
        return frame[4:]
    if linktype in (12, 14, 101, 228, 229):  # raw IP
        return frame
    if linktype == 113:  # Linux SLL
        return frame[16:]
    return None


def tcp_segment(ip: bytes):
    """Return (src, sport, dst, dport, seq, flags, payload) or None."""
    if not ip:
        return None
    ver = ip[0] >> 4
    if ver == 4:
        ihl = (ip[0] & 0xF) * 4
        if ip[9] != 6:
            return None
        total = struct.unpack_from(">H", ip, 2)[0]
        src, dst = ".".join(map(str, ip[12:16])), ".".join(map(str, ip[16:20]))
        tcp = ip[ihl:total]
    elif ver == 6:
        if ip[6] != 6:
            return None
        plen = struct.unpack_from(">H", ip, 4)[0]
        src, dst = ip[8:24].hex(), ip[24:40].hex()
        tcp = ip[40 : 40 + plen]
    else:
        return None
    sport, dport, seq = struct.unpack_from(">HHI", tcp, 0)
    doff = (tcp[12] >> 4) * 4
    return src, sport, dst, dport, seq, tcp[13], tcp[doff:]


# ---------------------------------------------------------------- reassembly

class Direction:
    def __init__(self) -> None:
        self.segments: list[tuple[float, int, bytes]] = []
        self.isn: int | None = None

    def stream(self) -> tuple[bytes, list[tuple[int, float]]]:
        """Reassemble in sequence order; return bytes and (offset, ts) marks."""
        if not self.segments:
            return b"", []
        base = self.isn if self.isn is not None else min(s for _, s, _ in self.segments)
        buf = bytearray()
        marks: list[tuple[int, float]] = []
        for ts, seq, payload in sorted(self.segments, key=lambda s: ((s[1] - base) & 0xFFFFFFFF, s[0])):
            rel = (seq - base) & 0xFFFFFFFF
            if rel + len(payload) <= len(buf):
                continue  # retransmission
            if rel > len(buf):
                marks.append((len(buf), ts))
                buf += b"\0" * (rel - len(buf))  # gap (missed packet)
            payload = payload[len(buf) - rel :]
            marks.append((len(buf), ts))
            buf += payload
        return bytes(buf), marks


def envelopes(buf: bytes, marks: list[tuple[int, float]]):
    off = 0
    while off + 4 <= len(buf):
        k, sl = struct.unpack_from(">HH", buf, off)
        ts = next((t for o, t in reversed(marks) if o <= off), 0.0)
        yield ts, k, buf[off + 4 : off + 4 + sl]
        off += 4 + sl


# ---------------------------------------------------------------- annotation

def describe_userdata(data: bytes, from_panel: bool, show_code: bool) -> list[str]:
    if len(data) == 1 and not from_panel:
        return [f"KEY {KEY_NAMES.get(data[0], f'0x{data[0]:02X}')}"]
    if len(data) > 3 and data[1] == 0x70:
        block = data[3 : 2 + data[0]]
        return describe_st70(block, data[2], show_code)
    return ["records " + " | ".join(r.hex(" ") for r in split_records(data))]


def describe_st70(block: bytes, geraet_frei: int, show_code: bool) -> list[str]:
    if not block:
        return ["St70 <empty>"]
    kind = block[0]
    name = ST70_NAMES.get(kind, f"0x{kind:02X}")
    if kind in (0x92, 0x94, 0x9E, 0x8A, 0x8C, 0x18, 0x98, 0x8E):
        code = block[1:4].hex(" ") if show_code else "** ** **"
        return [f"St70[{geraet_frei:02X}] {name} code={code} rest={block[4:].hex(' ')}"]
    if kind == 0x99 and len(block) > 2:
        lines = [f"St70[{geraet_frei:02X}] COM2BUS BT={block[1]:02X}"]
        lines += ["    " + describe_c2b(r) for r in split_records(block[2:])]
        return lines
    return [f"St70[{geraet_frei:02X}] {name} {block[1:].hex(' ')}"]


def describe_c2b(rec: bytes) -> str:
    kind = rec[0]
    if kind == 0x54:
        parts = []
        for marker, label in ((0x80, "L1"), (0xC0, "L2")):
            i = rec.find(bytes([0x1B, marker]))
            if i >= 0:
                parts.append(f'{label}="{lcd_decode(rec[i + 2 : i + 18])}"')
        return f"LCD {' '.join(parts)}  [{rec[1:].hex(' ')}]"
    if kind in (0xC0, 0xC1):
        return f"LEDS {'steady' if kind == 0xC0 else 'blink '} {rec[1:].hex(' ')}"
    if kind == 0xCA and len(rec) >= 3:
        bits = rec[1] | rec[2] << 8
        on = [str(i + 1) for i in range(16) if bits >> i & 1]
        return f"SWITCHES on={','.join(on) or '-'}"
    if kind == 0xCD:
        return f"LABEL {rec[1:4].hex(' ')} \"{lcd_decode(rec[4:])}\""
    if kind == 0xCE:
        return f"KEY_ACK {rec[1:].hex(' ')}"
    if kind == 0xCF:
        return f"PANEL_RESPONSE {rec[1:].hex(' ')}"
    return f"0x{kind:02X} {rec[1:].hex(' ')}"


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", type=Path)
    ap.add_argument("--port", type=int, help="panel TCP port (default: auto-detect)")
    ap.add_argument("--show-code", action="store_true", help="do not mask the keypad code")
    ap.add_argument("--no-key", action="store_true", help="do not ask for a Freischaltcode")
    args = ap.parse_args()

    flows: dict[tuple, Direction] = defaultdict(Direction)
    for ts, lt, frame in read_packets(args.pcap):
        seg = tcp_segment(ip_payload(lt, frame) or b"")
        if seg is None:
            continue
        src, sport, dst, dport, seq, flags, payload = seg
        if args.port and args.port not in (sport, dport):
            continue
        d = flows[(src, sport, dst, dport)]
        if flags & 0x02:  # SYN
            d.isn = (seq + 1) & 0xFFFFFFFF
        if payload:
            d.segments.append((ts, seq, payload))

    # A Telenot connection starts with the panel's plaintext SYNC_REQUEST (K=0).
    sessions = []
    for (src, sport, dst, dport), d in flows.items():
        buf, _ = d.stream()
        if len(buf) >= 21 and buf[:2] == b"\0\0" and buf[14] == 1:
            sessions.append(((src, sport), (dst, dport)))
    if not sessions:
        print("No Telenot session found. TCP flows with payload:")
        for (src, sport, dst, dport), d in sorted(flows.items()):
            n = sum(len(p) for _, _, p in d.segments)
            if n:
                print(f"  {src}:{sport} -> {dst}:{dport}  {n} bytes  first={d.stream()[0][:8].hex(' ')}")
        return

    fsc = key = None
    if not args.no_key:
        if aes := os.environ.get("TELENOT_AES_KEY"):
            key = bytes.fromhex(aes)
        else:
            raw = os.environ.get("TELENOT_FREISCHALTCODE") or getpass.getpass("Freischaltcode (16 hex, empty to skip): ")
            fsc = bytes.fromhex(raw.strip()) if raw.strip() else None

    for panel, app in sessions:
        print(f"\n=== session panel {panel[0]}:{panel[1]} <-> app {app[0]}:{app[1]} ===")
        merged = []
        for side, d in (("P>A", flows[(*panel, *app)]), ("A>P", flows[(*app, *panel)])):
            merged += [(ts, side, k, p) for ts, k, p in envelopes(*d.stream())]
        merged.sort(key=lambda e: e[0])
        session_key = key
        t0 = merged[0][0]
        for ts, side, k, payload in merged:
            stamp = f"{ts - t0:8.3f}s"
            if k == KEY_NUMBER_PLAIN:
                plain = unseal(k, payload, None)
            elif session_key is None:
                print(f"{stamp} {side} K={k} {len(payload)} bytes encrypted (no key)")
                continue
            else:
                plain = unseal(k, payload, session_key)
            if plain is None:
                print(f"{stamp} {side} K={k} {len(payload)} bytes  !! checksum/decrypt failed")
                continue
            tg = S2Telegram.decode(plain)
            if tg.ik == 1 and k == KEY_NUMBER_PLAIN and fsc is not None:
                session_key = derive_betreiber_key(fsc, tg.tc)
            nk = NK_NAMES.get(tg.nk, f"NK=0x{tg.nk:02X}") if tg.pk == 0x82 else "VdS"
            head = f"{stamp} {side} K={k:<5} TC={tg.tc:08X} RC={tg.rc:08X} {IK_NAMES.get(tg.ik, tg.ik):<11} {nk}"
            if tg.ik in (1, 2):
                head += f" data={tg.data.hex(' ')}" if tg.data else ""
                print(head)
                continue
            if not tg.data:
                print(head)
                continue
            if tg.nk in (5, 6):
                print(f"{head} {tg.data.hex(' ')}")
                continue
            lines = describe_userdata(tg.data, side == "P>A", args.show_code)
            print(f"{head} {lines[0]}")
            for line in lines[1:]:
                print(f"{'':10}{line}")
    print(f"\n(capture start {datetime.fromtimestamp(merged[0][0]).isoformat(timespec='seconds')})")


if __name__ == "__main__":
    main()
