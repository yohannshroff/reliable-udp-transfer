"""Packet framing shared by both the baseline and improved protocols.

Wire format (network byte order), fixed 13-byte header + variable payload:

    | seq_num (4B) | ack_num (4B) | flags (1B) | checksum (2B) | payload_len (2B) | payload (variable) |

`checksum` is the 16-bit one's-complement Internet checksum computed over the
whole header (with the checksum field zeroed) plus the payload.

This module MUST stay byte-for-byte identical between the two protocol versions
so that any measured performance difference is attributable only to the
ARQ / RTO changes and not to incidental framing differences.
"""

import struct
from collections import namedtuple

# ---------------------------------------------------------------------------
# Flag bits
# ---------------------------------------------------------------------------
DATA = 0x01
ACK = 0x02
ACK2 = 0x04
NAK = 0x08
FIN = 0x10

_FLAG_NAMES = [
    (DATA, "DATA"),
    (ACK, "ACK"),
    (ACK2, "ACK2"),
    (NAK, "NAK"),
    (FIN, "FIN"),
]

HEADER_FORMAT = "!IIBHH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 13 bytes
assert HEADER_SIZE == 13

# Max UDP payload we are willing to emit. 1024 keeps us well under the typical
# 1500-byte Ethernet MTU even after the 13-byte header and IP/UDP overhead.
MAX_PAYLOAD = 1024

Packet = namedtuple(
    "Packet", ["seq", "ack", "flags", "checksum_valid", "payload"]
)


def flags_to_str(flags):
    """Human-readable flag list, e.g. 0x12 -> 'DATA|FIN'."""
    names = [name for bit, name in _FLAG_NAMES if flags & bit]
    return "|".join(names) if names else "0"


def _internet_checksum(data):
    """16-bit one's-complement sum of `data` (padded to even length)."""
    if len(data) & 1:
        data = data + b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def encode(seq, ack, flags, payload=b""):
    """Serialise a packet to bytes, filling in payload_len and checksum."""
    if payload is None:
        payload = b""
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for 16-bit length field")

    header_no_ck = struct.pack(
        HEADER_FORMAT, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, flags & 0xFF, 0, len(payload)
    )
    checksum = _internet_checksum(header_no_ck + payload)
    header = struct.pack(
        HEADER_FORMAT,
        seq & 0xFFFFFFFF,
        ack & 0xFFFFFFFF,
        flags & 0xFF,
        checksum,
        len(payload),
    )
    return header + payload


def decode(data):
    """Parse bytes into a Packet. `checksum_valid` is False on any corruption.

    Returns None only when the buffer is too short to contain a header.
    """
    if len(data) < HEADER_SIZE:
        return None

    seq, ack, flags, checksum, payload_len = struct.unpack(
        HEADER_FORMAT, data[:HEADER_SIZE]
    )
    payload = data[HEADER_SIZE:]

    # Recompute over header-with-zeroed-checksum + actual payload bytes.
    header_no_ck = struct.pack(
        HEADER_FORMAT, seq, ack, flags, 0, payload_len
    )
    recomputed = _internet_checksum(header_no_ck + payload)

    checksum_valid = (recomputed == checksum) and (payload_len == len(payload))
    return Packet(seq=seq, ack=ack, flags=flags, checksum_valid=checksum_valid, payload=payload)
