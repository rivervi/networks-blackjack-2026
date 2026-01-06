#!/usr/bin/env python3
"""
server.py — Blackjack server (dealer), upgraded for:
- Better timeouts (no random disconnects when a user pauses)
- Compatibility with strict binary protocol AND tolerant text fallbacks
- Cleaner logs + fun output
- Per-client stats

Runs forever until Ctrl+C.
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from common import (
    UDP_OFFER_PORT,
    MAGIC_COOKIE_BYTES,
    TYPE_PAYLOAD,
    pack_offer,
    unpack_request,
    unpack_payload,
    pack_payload,
    safe_local_ip,
    SocketBuffer,
    Card,
    hand_total_simplified,
    REQUEST_STRUCT,
    PAYLOAD_STRUCT,
    RESULT_ONGOING,
    RESULT_TIE,
    RESULT_LOSS,
    RESULT_WIN,
)

DECISION_HIT = b"Hittt"
DECISION_STAND = b"Stand"
SERVER_DECISION_FILL = b"-----"


# =========================
# Game mechanics
# =========================
class Deck:
    def __init__(self) -> None:
        self.cards: List[Card] = [Card(rank=r, suit=s) for s in range(4) for r in range(1, 14)]
        random.shuffle(self.cards)

    def draw(self) -> Card:
        if not self.cards:
            self.__init__()
        return self.cards.pop()


@dataclass
class ClientStats:
    name: str
    rounds_requested: int = 0
    rounds_played: int = 0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    player_busts: int = 0
    dealer_busts: int = 0
    total_player_final: int = 0
    total_dealer_final: int = 0
    total_player_hits: int = 0

    def record_result(self, result: int, player_total: int, dealer_total: int, player_bust: bool, dealer_bust: bool, player_hits: int) -> None:
        self.rounds_played += 1
        self.total_player_final += player_total
        self.total_dealer_final += dealer_total
        self.total_player_hits += player_hits
        if player_bust:
            self.player_busts += 1
        if dealer_bust:
            self.dealer_busts += 1

        if result == RESULT_WIN:
            self.wins += 1
        elif result == RESULT_LOSS:
            self.losses += 1
        elif result == RESULT_TIE:
            self.ties += 1

    def summary_line(self) -> str:
        if self.rounds_played == 0:
            return f"{self.name}: (no completed rounds)"
        wr = self.wins / self.rounds_played
        avg_p = self.total_player_final / self.rounds_played
        avg_d = self.total_dealer_final / self.rounds_played
        avg_hits = self.total_player_hits / self.rounds_played
        return (
            f"{self.name}: played={self.rounds_played}, W={self.wins}, L={self.losses}, T={self.ties}, "
            f"WR={wr:.1%}, busts(P/D)={self.player_busts}/{self.dealer_busts}, "
            f"avg_total(P/D)={avg_p:.1f}/{avg_d:.1f}, avg_hits={avg_hits:.2f}"
        )


# =========================
# Network: UDP offers
# =========================
def broadcast_offers(stop_event: threading.Event, tcp_port: int, server_name: str, interval: float) -> None:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    offer = pack_offer(tcp_port, server_name)
    dest = ("255.255.255.255", UDP_OFFER_PORT)

    while not stop_event.is_set():
        try:
            udp.sendto(offer, dest)
        except Exception as e:
            print(f"[UDP] Offer send failed: {e}")
        time.sleep(interval)

    try:
        udp.close()
    except Exception:
        pass


# =========================
# Flexible request / decision parsing
# =========================
def read_request_flexible(rb: SocketBuffer, request_timeout: float) -> Optional[Tuple[int, str]]:
    """
    Accepts:
    - strict binary request (38 bytes) per spec
    - OR legacy text request like: "5\\n"  or "5 TeamName\\n"
      (some teams follow the example narrative rather than the struct)
    """
    try:
        first4 = rb.peek_exact(4, request_timeout)
    except (TimeoutError, EOFError):
        return None

    if first4 == MAGIC_COOKIE_BYTES:
        try:
            data = rb.read_exact(REQUEST_STRUCT.size, request_timeout)
        except (TimeoutError, EOFError):
            return None
        parsed = unpack_request(data)
        return parsed

    # Text fallback:
    try:
        line = rb.read_line(request_timeout, maxlen=128)
    except (TimeoutError, EOFError):
        return None

    line = line.strip()
    if not line:
        return None

    parts = line.decode("utf-8", errors="ignore").strip().split()
    try:
        rounds = int(parts[0])
    except Exception:
        return None

    name = "LegacyClient"
    if len(parts) >= 2:
        name = " ".join(parts[1:])[:32]
    rounds = max(1, min(255, rounds))
    return rounds, name


def read_decision_flexible(rb: SocketBuffer, decision_timeout: float) -> bytes:
    """
    Accepts:
    - strict binary payload decision (14 bytes) per spec
    - OR text line: "hit\\n" / "stand\\n" (for compatibility)
    On timeout: returns Stand (friendly behavior).
    """
    # If nothing arrives in time, we auto-stand to avoid killing the session.
    try:
        head = rb.peek_exact(5, decision_timeout)  # cookie(4) + type(1)
    except TimeoutError:
        return DECISION_STAND
    except EOFError:
        raise

    if head[:4] == MAGIC_COOKIE_BYTES and head[4] == TYPE_PAYLOAD:
        data = rb.read_exact(PAYLOAD_STRUCT.size, decision_timeout)
        parsed = unpack_payload(data)
        if not parsed:
            return DECISION_STAND
        decision5, _res, _card = parsed
        # Normalize common variants
        d = decision5.strip().lower()
        if d.startswith(b"hit"):
            return DECISION_HIT
        if d.startswith(b"sta"):
            return DECISION_STAND
        return DECISION_STAND

    # Text fallback:
    line = rb.read_line(decision_timeout, maxlen=64).strip().lower()
    if line.startswith(b"hit"):
        return DECISION_HIT
    if line.startswith(b"sta"):
        return DECISION_STAND
    return DECISION_STAND


# =========================
# Game I/O helpers
# =========================
def send_card(sock: socket.socket, card: Card, result: int = RESULT_ONGOING) -> None:
    msg = pack_payload(SERVER_DECISION_FILL, result, card)
    sock.sendall(msg)


def send_result(sock: socket.socket, result: int) -> None:
    msg = pack_payload(SERVER_DECISION_FILL, result, None)
    sock.sendall(msg)


# =========================
# Round logic
# =========================
def play_one_round(conn: socket.socket, rb: SocketBuffer, stats: ClientStats, decision_timeout: float) -> None:
    deck = Deck()
    player: List[Card] = []
    dealer: List[Card] = []
    player_hits = 0

    # Initial deal (fresh deck each round)
    player.append(deck.draw())
    player.append(deck.draw())
    dealer.append(deck.draw())  # face-up
    dealer.append(deck.draw())  # hidden

    # Send: player card, player card, dealer up-card
    send_card(conn, player[0])
    send_card(conn, player[1])
    send_card(conn, dealer[0])

    player_total = hand_total_simplified(player)
    dealer_total = dealer[0].points_simplified()  # visible only

    print(f"  🎴 Player: {player[0].pretty()} + {player[1].pretty()}  => {player_total}")
    print(f"  🕵️ Dealer shows: {dealer[0].pretty()}  (visible {dealer_total})")

    # Player turn
    player_bust = False
    while True:
        if player_total > 21:
            player_bust = True
            break

        try:
            decision = read_decision_flexible(rb, decision_timeout)
        except EOFError:
            raise

        if decision == DECISION_HIT:
            player_hits += 1
            card = deck.draw()
            player.append(card)
            player_total += card.points_simplified()
            print(f"  ➕ Player HIT -> {card.pretty()}  => {player_total}")

            if player_total > 21:
                # Bust: send busting card with LOSS (single end-of-round message)
                send_card(conn, card, RESULT_LOSS)
                player_bust = True
                break
            else:
                send_card(conn, card, RESULT_ONGOING)
                continue

        # Stand (or unknown -> stand)
        print(f"  ✋ Player STAND at {player_total}")
        break

    if player_bust:
        # Dealer wins
        print("  💥 Player busts! Dealer wins.")
        stats.record_result(RESULT_LOSS, player_total, hand_total_simplified(dealer), True, False, player_hits)
        return

    # Dealer turn
    # Reveal hidden
    send_card(conn, dealer[1], RESULT_ONGOING)
    dealer_total = hand_total_simplified(dealer)
    print(f"  🃏 Dealer reveals: {dealer[1].pretty()}  => {dealer_total}")

    dealer_bust = False
    while dealer_total < 17:
        card = deck.draw()
        dealer.append(card)
        dealer_total += card.points_simplified()
        print(f"  ➕ Dealer HIT -> {card.pretty()}  => {dealer_total}")

        if dealer_total > 21:
            # Dealer bust: send busting card with WIN (single end-of-round message)
            send_card(conn, card, RESULT_WIN)
            dealer_bust = True
            print("  💥 Dealer busts! Player wins.")
            stats.record_result(RESULT_WIN, player_total, dealer_total, False, True, player_hits)
            return

        send_card(conn, card, RESULT_ONGOING)

    print(f"  ✋ Dealer STAND at {dealer_total}")

    # Decide winner (no player bust and no dealer bust here)
    if player_total > dealer_total:
        result = RESULT_WIN
        print("  🏆 Player total higher -> WIN")
    elif dealer_total > player_total:
        result = RESULT_LOSS
        print("  ☠️ Dealer total higher -> LOSS")
    else:
        result = RESULT_TIE
        print("  🤝 Equal totals -> TIE")

    send_result(conn, result)
    stats.record_result(result, player_total, dealer_total, False, dealer_bust, player_hits)


# =========================
# Client handler
# =========================
def handle_client(conn: socket.socket, addr: Tuple[str, int], args: argparse.Namespace) -> None:
    client_ip, client_port = addr
    rb = SocketBuffer(conn)

    print(f"[TCP] Client connected from {client_ip}:{client_port}")

    # Request phase: short timeout to prevent hanging forever on half-open connections
    try:
        conn.settimeout(None)  # we handle timeouts via select in SocketBuffer
        req = read_request_flexible(rb, args.request_timeout)
    except Exception:
        req = None

    if not req:
        print(f"[TCP] Invalid/missing request from {client_ip}:{client_port} — closing")
        try:
            conn.close()
        except Exception:
            pass
        return

    rounds, client_name = req
    rounds = int(rounds)
    stats = ClientStats(name=client_name, rounds_requested=rounds)

    # Drain stray newlines if client sent binary request + "\n"
    rb.drain_whitespace_nonblocking()

    print(f"[GAME] '{client_name}' requested {rounds} rounds ✅")

    try:
        for r in range(1, rounds + 1):
            print(f"\n[ROUND {r}/{rounds}] Dealer vs '{client_name}'")
            play_one_round(conn, rb, stats, args.decision_timeout)
    except (EOFError, ConnectionResetError, BrokenPipeError) as e:
        print(f"[TCP] '{client_name}' disconnected: {e}")
    except TimeoutError as e:
        print(f"[TCP] '{client_name}' timed out: {e}")
    except Exception as e:
        print(f"[ERR] Unexpected error with '{client_name}': {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        print(f"\n[STATS] {stats.summary_line()}")
        print(f"[TCP] Connection with '{client_name}' closed\n")


# =========================
# Main
# =========================
def banner(name: str) -> None:
    print("===============================================")
    print("🃏  BLACKJACK SERVER  🃏")
    print(f"Dealer Team: {name}")
    print("Broadcasting UDP offers + accepting TCP clients")
    print("===============================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blackjack Server (robust + interoperable)")
    parser.add_argument("--name", default="TeamServer", help="Server team name (<=32 bytes)")
    parser.add_argument("--tcp-port", type=int, default=0, help="TCP port to listen on (0 = auto)")
    parser.add_argument("--offer-interval", type=float, default=1.0, help="Seconds between UDP offers")
    parser.add_argument("--request-timeout", type=float, default=15.0, help="Seconds to wait for request")
    parser.add_argument("--decision-timeout", type=float, default=120.0, help="Seconds to wait per player decision (timeout => auto-stand)")
    args = parser.parse_args()

    banner(args.name)

    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind(("", args.tcp_port))
    tcp.listen()
    tcp_port = tcp.getsockname()[1]

    ip = safe_local_ip()
    print(f"Server started, listening on IP address {ip} (TCP port {tcp_port})")

    stop_event = threading.Event()
    t = threading.Thread(
        target=broadcast_offers,
        args=(stop_event, tcp_port, args.name, args.offer_interval),
        daemon=True,
    )
    t.start()

    try:
        while True:
            conn, addr = tcp.accept()
            th = threading.Thread(target=handle_client, args=(conn, addr, args), daemon=True)
            th.start()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        stop_event.set()
        try:
            tcp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
