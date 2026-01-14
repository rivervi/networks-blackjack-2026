

from __future__ import annotations

import argparse
import socket
from dataclasses import dataclass
from typing import Optional, Tuple, List

from common import (
    UDP_OFFER_PORT,
    unpack_offer,
    pack_request,
    pack_payload,
    unpack_payload,
    SocketBuffer,
    Card,
    hand_total_simplified,
    PAYLOAD_STRUCT,
    RESULT_ONGOING,
    RESULT_TIE,
    RESULT_LOSS,
    RESULT_WIN,
)

DECISION_HIT = b"Hittt"
DECISION_STAND = b"Stand"



@dataclass
class SessionStats:
    rounds: int = 0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    player_busts: int = 0
    dealer_busts: int = 0
    total_player_final: int = 0
    total_dealer_final: int = 0
    total_hits: int = 0
    best_player_total: int = 0
    worst_player_total: int = 999

    def update(self, result: int, player_total: int, dealer_total: int, player_bust: bool, dealer_bust: bool, hits: int) -> None:
        self.rounds += 1
        self.total_player_final += player_total
        self.total_dealer_final += dealer_total
        self.total_hits += hits
        self.best_player_total = max(self.best_player_total, player_total)
        self.worst_player_total = min(self.worst_player_total, player_total)
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

    def summary(self) -> str:
        if self.rounds == 0:
            return "No completed rounds."
        wr = self.wins / self.rounds
        avgp = self.total_player_final / self.rounds
        avgd = self.total_dealer_final / self.rounds
        avgh = self.total_hits / self.rounds
        return (
            f"Finished playing {self.rounds} rounds\n"
            f"  ✅ Wins: {self.wins}  ❌ Losses: {self.losses}  🤝 Ties: {self.ties}\n"
            f"  📈 Win rate: {wr:.2%}\n"
            f"  💥 Busts (You/Dealer): {self.player_busts}/{self.dealer_busts}\n"
            f"  🔢 Avg totals (You/Dealer): {avgp:.1f}/{avgd:.1f}\n"
            f"  👊 Avg hits per round: {avgh:.2f}\n"
            f"  🌟 Best/Worst player total: {self.best_player_total}/{self.worst_player_total}\n"
        )



def result_str(res: int) -> str:
    return {RESULT_WIN: "WIN 🏆", RESULT_LOSS: "LOSS ☠️", RESULT_TIE: "TIE 🤝"}.get(res, f"RES({res})")


def ask_rounds() -> int:
    while True:
        s = input("How many rounds do you want to play? (1-255): ").strip()
        try:
            n = int(s)
            if 1 <= n <= 255:
                return n
        except ValueError:
            pass
        print("Please enter an integer between 1 and 255.")


def ask_decision() -> bytes:
    while True:
        s = input("Hit or stand? ").strip().lower()
        if s in ("hit", "h"):
            return DECISION_HIT
        if s in ("stand", "s"):
            return DECISION_STAND
        print("Please type 'hit' or 'stand'.")


def auto_decision(total: int) -> bytes:
    """
    Simple autoplayer:
    - hit below 17
    - stand on 17+
    """
    return DECISION_HIT if total < 17 else DECISION_STAND



def recv_payload(rb: SocketBuffer, timeout: float) -> Tuple[bytes, int, Card]:
    """
    Receives one payload. If invalid/corrupt, raises ValueError.
    """
    data = rb.read_exact(PAYLOAD_STRUCT.size, timeout)
    parsed = unpack_payload(data)
    if not parsed:
        raise ValueError("Invalid payload received")
    return parsed



def listen_for_offer() -> Tuple[str, int, str]:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except Exception:
        # Windows may not support SO_REUSEPORT; ignore
        pass

    udp.bind(("", UDP_OFFER_PORT))
    print("Client started, listening for offer requests...")

    while True:
        data, (ip, _port) = udp.recvfrom(2048)
        parsed = unpack_offer(data)
        if not parsed:
            continue
        tcp_port, server_name = parsed
        print(f"Received offer from {ip} (server='{server_name}', tcp_port={tcp_port})")
        udp.close()
        return ip, tcp_port, server_name



def play_session(server_ip: str, tcp_port: int, team_name: str, rounds: int, timeout: float, autopilot: bool) -> None:
    print(f"[TCP] Connecting to server {server_ip}:{tcp_port} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((server_ip, tcp_port))
    rb = SocketBuffer(s)

    # Send request (strict binary)
    s.sendall(pack_request(rounds, team_name))

    stats = SessionStats()

    try:
        for r in range(1, rounds + 1):
            print(f"\n==================== ROUND {r}/{rounds} ====================")

            player_hand: List[Card] = []
            dealer_hand: List[Card] = []
            player_hits = 0
            player_bust = False
            dealer_bust = False

            # Initial: expect at least 3 cards (player, player, dealer-up)
            # Be tolerant: keep reading until we have these.
            while len(player_hand) < 2 or len(dealer_hand) < 1:
                _dec, res, card = recv_payload(rb, timeout)
                if res != RESULT_ONGOING:
                    # Some servers might immediately finalize; handle gracefully
                    print(f"[ROUND] Immediate result: {result_str(res)}")
                    stats.update(res, 0, 0, False, False, 0)
                    break

                if card.rank == 0:
                    continue

                if len(player_hand) < 2:
                    player_hand.append(card)
                else:
                    dealer_hand.append(card)

            if len(player_hand) < 2 or len(dealer_hand) < 1:
                # Round ended weirdly; continue
                continue

            player_total = hand_total_simplified(player_hand)
            dealer_visible = hand_total_simplified(dealer_hand)

            print(f"[PLAYER] You got: {player_hand[0].pretty()} and {player_hand[1].pretty()} (total {player_total})")
            print(f"[DEALER] Dealer shows: {dealer_hand[0].pretty()} (visible {dealer_visible})")

            # Player turn
            round_result: Optional[int] = None
            while True:
                decision = auto_decision(player_total) if autopilot else ask_decision()
                s.sendall(pack_payload(decision, 0, None))

                if decision == DECISION_HIT:
                    player_hits += 1
                    _dec2, res2, new_card = recv_payload(rb, timeout)
                    if new_card.rank != 0:
                        player_hand.append(new_card)
                        player_total = hand_total_simplified(player_hand)
                        print(f"[PLAYER] Hit -> {new_card.pretty()} (total {player_total})")

                    if res2 != RESULT_ONGOING:
                        print(f"[ROUND] Result: {result_str(res2)}")
                        round_result = res2
                        if res2 == RESULT_LOSS:
                            player_bust = (player_total > 21)
                        break
                    continue

                # Stand: read dealer stream until final result
                print("[PLAYER] Stand. Dealer's turn...")
                while True:
                    _dec2, res2, card2 = recv_payload(rb, timeout)
                    if card2.rank != 0:
                        dealer_hand.append(card2)
                        dealer_total_now = hand_total_simplified(dealer_hand)
                        print(f"[DEALER] Reveals/draws -> {card2.pretty()} (dealer total {dealer_total_now})")

                    if res2 != RESULT_ONGOING:
                        print(f"[ROUND] Result: {result_str(res2)}")
                        round_result = res2
                        dealer_total_now = hand_total_simplified(dealer_hand)
                        if res2 == RESULT_WIN:
                            dealer_bust = (dealer_total_now > 21)
                        break
                break

            if round_result is None:
                print("[WARN] Round ended without explicit result (server mismatch).")
                continue

            dealer_final = hand_total_simplified(dealer_hand)
            stats.update(
                round_result,
                player_total,
                dealer_final,
                player_bust=(player_total > 21),
                dealer_bust=(dealer_final > 21),
                hits=player_hits,
            )

    except (TimeoutError, EOFError, ConnectionResetError, BrokenPipeError, OSError) as e:
        print(f"[ERR] Session failed: {e}")
    except ValueError as e:
        print(f"[ERR] Bad data from server: {e}")
    finally:
        try:
            s.close()
        except Exception:
            pass

    print("\n" + stats.summary())



def parse_direct(d: str) -> Tuple[str, int]:
    # format: IP:PORT
    if ":" not in d:
        raise ValueError("direct must be IP:PORT")
    ip, port_s = d.rsplit(":", 1)
    port = int(port_s)
    return ip, port



def banner(name: str) -> None:
    print("===============================================")
    print("🎰  BLACKJACK CLIENT  🎰")
    print(f"Player Team: {name}")
    print("Listening for offers or using --direct")
    print("===============================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blackjack Client (robust + fun)")
    parser.add_argument("--name", default="TeamClient", help="Client team name (<=32 bytes)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for server messages")
    parser.add_argument("--direct", default=None, help="Direct connect without UDP, format IP:PORT (e.g. 127.0.0.1:54473)")
    parser.add_argument("--auto", action="store_true", help="Autoplay using a simple strategy (for fast testing)")
    args = parser.parse_args()

    banner(args.name)

    while True:
        try:
            if args.direct:
                server_ip, tcp_port = parse_direct(args.direct)
                server_name = "(direct)"
            else:
                server_ip, tcp_port, server_name = listen_for_offer()

            rounds = ask_rounds()
            play_session(server_ip, tcp_port, args.name, rounds, args.timeout, args.auto)

        except KeyboardInterrupt:
            print("\nBye 👋")
            return
        except Exception as e:
            print(f"[ERR] Unexpected: {e}")
            # Go back to offer listening / prompt loop


if __name__ == "__main__":
    main()
