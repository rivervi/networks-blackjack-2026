Blackjack Client-Server (Hackathon)

This project contains a TCP/UDP client-server implementation of a simplified Blackjack game.

Files:
- server.py  : starts the server, broadcasts offers over UDP, and hosts games over TCP
- client.py  : listens for offers, connects over TCP, and plays the game (not committed yet)
- common.py  : shared protocol definitions and helpers

How to run:
1. Start the server:
   python server.py --name "TeamServer"

2. Start the client in a separate terminal:
   python client.py --name "TeamClient"

Notes:
- The client listens for server offers on UDP port 13122.
- The server chooses a TCP port automatically and includes it in the offer message.
- Python 3 is required.
