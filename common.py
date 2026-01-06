#!/usr/bin/env python3
"""
common.py — Shared constants + packet encode/decode + safe buffered I/O helpers.

Protocol (network byte order):
Offer (UDP, server -> client):  cookie(4) type(1) tcp_port(2) server_name(32)  = 39 bytes
Request (TCP, client -> server): cookie(4) type(1) rounds(1) client_name(32)   = 38 bytes
Payload (TCP, both directions):  cookie(4) type(1) decision(5) result(1) rank(2) suit(1) = 14 bytes

Notes:
- decision is ASCII bytes "Hittt" or "Stand" (exactly 5 bytes)
- result: 0x0 ongoing, 0x1 tie, 0x2 loss, 0x3 win
- rank: 1..13 (Ace=1, J=11, Q=12, K=13)
- suit: 0..3 encoded as HDCS (Heart, Diamond, Club, Spade)
"""

from __future__ import annotations

import struct
import socket
import select
import time
from dataclasses import dataclass
from typing import Optional, Tuple

# =========================
# Protocol constants
# =========================
MAGIC_COOKIE = 0xABCDDCBA
MAGIC_COOKIE_BYTES = struct.pack("!I", MAGIC_COOKIE)

TYPE_OFFER = 0x2
TYPE_REQUEST = 0x3
TYPE_PAYLOAD = 0x4

RESULT_ONGOING = 0x0
RESULT_TIE = 0x1
RESULT_LOSS = 0x2
RESULT_WIN = 0x3

UDP_OFFER_PORT = 13122  # client listens here (hardcoded by spec)

# Structs
OFFER_STRUCT = struct.Struct("!IBH32s")      # cookie, type, tcp_port, server_name
REQUEST_STRUCT = struct.Struct("!IBB32s")    # cookie, type, rounds, client_name
PAYLOAD_STRUCT = struct.Struct("!IB5sBHB")   # cookie, type, decision, result, rank, suit

# Suits are encoded as HDCS (Heart, Diamond, Club, Spade)
SUITS = ["Heart", "Diamond", "Club", "Spade"]


# =========================
# Card & helpers
# =========================
@dataclass(frozen=True)
class Card:
    rank: int  # 1..13
    suit: int  # 0..3

    def points_simplified(self) -> int:
        """Simplified blackjack points (Ace=11 always, J/Q/K=10)."""
        if self.rank == 1:
            return 11
        if 2 <= self.rank <= 10:
            return self.rank
        return 10

    def pretty(self) -> str:
        rank_str = {1: "A", 11: "J", 12: "Q", 13: "K"}.get(self.rank, str(self.rank))
        suit_str = SUITS[self.suit] if 0 <= self.suit <= 3 else f"Suit({self.suit})"
        return f"{rank_str} of {suit_str}"


def hand_total_simplified(hand: list[Card]) -> int:
    """Sum points with Ace always 11 (per assignment simplified rules)."""
    return sum(c.points_simplified() for c in hand)


# =========================
# Fixed-length names
# =========================
def _fixed_name_bytes(name: str) -> bytes:
    """Encode name to exactly 32 bytes, padded with 0x00 or truncated."""
    raw = name.encode("utf-8", errors="ignore")[:32]
    return raw.ljust(32, b"\x00")


def _name_from_fixed(b: bytes) -> str:
    """Decode fixed 32 bytes name (strip null padding)."""
    return b.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


# =========================
# Packet packing/unpacking
# =========================
def pack_offer(tcp_port: int, server_name: str) -> bytes:
    return OFFER_STRUCT.pack(MAGIC_COOKIE, TYPE_OFFER, tcp_port, _fixed_name_bytes(server_name))


def unpack_offer(data: bytes) -> Optional[Tuple[int, str]]:
    if len(data) != OFFER_STRUCT.size:
        return None
    cookie, mtype, tcp_port, sname = OFFER_STRUCT.unpack(data)
    if cookie != MAGIC_COOKIE or mtype != TYPE_OFFER:
        return None
    return tcp_port, _name_from_fixed(sname)


def pack_request(rounds: int, client_name: str) -> bytes:
    rounds = max(0, min(255, int(rounds)))
    return REQUEST_STRUCT.pack(MAGIC_COOKIE, TYPE_REQUEST, rounds, _fixed_name_bytes(client_name))


def unpack_request(data: bytes) -> Optional[Tuple[int, str]]:
    if len(data) != REQUEST_STRUCT.size:
        return None
    cookie, mtype, rounds, cname = REQUEST_STRUCT.unpack(data)
    if cookie != MAGIC_COOKIE or mtype != TYPE_REQUEST:
        return None
    return rounds, _name_from_fixed(cname)


def pack_payload(decision5: bytes, result: int, card: Optional[Card]) -> bytes:
    """
    decision5 must be exactly 5 bytes.
    For server->client messages, decision can be b"-----" (ignored by client).
    For client->server messages, result/rank/suit can be 0.
    """
    if len(decision5) != 5:
        raise ValueError("decision must be exactly 5 bytes")
    rank = card.rank if card else 0
    suit = card.suit if card else 0
    return PAYLOAD_STRUCT.pack(
        MAGIC_COOKIE,
        TYPE_PAYLOAD,
        decision5,
        result & 0xFF,
        rank & 0xFFFF,
        suit & 0xFF,
    )


def unpack_payload(data: bytes) -> Optional[Tuple[bytes, int, Card]]:
    if len(data) != PAYLOAD_STRUCT.size:
        return None
    cookie, mtype, decision5, result, rank, suit = PAYLOAD_STRUCT.unpack(data)
    if cookie != MAGIC_COOKIE or mtype != TYPE_PAYLOAD:
        return None
    card = Card(rank=rank, suit=suit)
    return decision5, result, card


# =========================
# Buffered socket reader
# =========================
class SocketBuffer:
    """
    A small buffered reader around a TCP socket that helps us:
    - read exact N bytes with a timeout (without busy-waiting)
    - peek into buffered bytes
    - read lines for legacy / text-based fallback compatibility
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def _wait_readable(self, timeout: Optional[float]) -> None:
        if timeout is None:
            r, _, _ = select.select([self.sock], [], [])
        else:
            r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            raise TimeoutError("timeout waiting for data")

    def _recv_some(self, timeout: Optional[float]) -> None:
        self._wait_readable(timeout)
        chunk = self.sock.recv(4096)
        if not chunk:
            raise EOFError("connection closed")
        self.buf.extend(chunk)

    def read_exact(self, n: int, timeout: Optional[float]) -> bytes:
        if n <= 0:
            return b""
        deadline = None if timeout is None else (time.monotonic() + timeout)

        while len(self.buf) < n:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._recv_some(remaining)

        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def peek_exact(self, n: int, timeout: Optional[float]) -> bytes:
        if n <= 0:
            return b""
        deadline = None if timeout is None else (time.monotonic() + timeout)

        while len(self.buf) < n:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._recv_some(remaining)

        return bytes(self.buf[:n])

    def read_line(self, timeout: Optional[float], maxlen: int = 512) -> bytes:
        """
        Read until '\n' (inclusive) or until maxlen is reached.
        Useful to tolerate teams that send "5\\n" requests or "hit\\n" decisions.
        """
        deadline = None if timeout is None else (time.monotonic() + timeout)

        while True:
            nl = self.buf.find(b"\n")
            if nl != -1:
                take = nl + 1
                out = bytes(self.buf[:take])
                del self.buf[:take]
                return out

            if len(self.buf) >= maxlen:
                out = bytes(self.buf[:maxlen])
                del self.buf[:maxlen]
                return out

            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._recv_some(remaining)

    def drain_whitespace_nonblocking(self, max_bytes: int = 1024) -> None:
        """
        Drain immediately available bytes that are whitespace.
        This helps if a client sends a trailing '\\n' after a binary request.
        Non-blocking: we only read what is already available.
        """
        # Peek at readiness with 0 timeout
        try:
            r, _, _ = select.select([self.sock], [], [], 0.0)
        except Exception:
            return
        if not r:
            return

        try:
            chunk = self.sock.recv(max_bytes)
        except Exception:
            return
        if not chunk:
            return

        # If it's all whitespace, drop it. Otherwise, keep it in buffer.
        if all(c in b" \t\r\n" for c in chunk):
            return
        self.buf.extend(chunk)


# =========================
# Misc utilities
# =========================
def safe_local_ip() -> str:
    """
    Best-effort way to get a non-loopback local IP for printing.
    Works even without internet in many LAN setups.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"
