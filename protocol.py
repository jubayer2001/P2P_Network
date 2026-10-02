"""
protocol.py
===========
Application-level protocol for the P2P network.

WHY THIS FILE EXISTS
--------------------
TCP is a *byte stream*. If Alice calls send() twice, Bob might receive both
messages glued together in ONE recv(), or one message split across TWO recv()s.
TCP does NOT remember where one message ended and the next began.

So we define our own rule ("message framing"):

    +----------------+---------------------------+
    | 4-byte length  |  JSON message (N bytes)   |
    +----------------+---------------------------+

The receiver first reads exactly 4 bytes (the length N), then reads exactly
N more bytes (the JSON). Now message boundaries are always known.

FILE TRANSFER uses the same idea:
    [4-byte length][JSON metadata: filename, filesize]  <- a normal framed message
    [raw file bytes ... exactly `filesize` bytes]       <- NOT JSON, just bytes
"""

import json
import struct

# ---------------------------------------------------------------------------
# Message type names (so we never mistype a string elsewhere in the code)
# ---------------------------------------------------------------------------
MSG_HELLO = "hello"
MSG_HELLO_ACK = "hello_ack"
MSG_TEXT = "text"
MSG_FILE = "file"
MSG_ERROR = "error"   # used to explain WHY a handshake was refused

# "!I" = network byte order (big-endian), unsigned 32-bit integer = 4 bytes
HEADER_FORMAT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # == 4

# Safety limit: refuse absurdly large JSON messages (protects against garbage
# data or a misbehaving peer). Real JSON messages here are tiny.
MAX_JSON_SIZE = 1024 * 1024  # 1 MB

# File data is sent in chunks so we never load a whole video into memory.
CHUNK_SIZE = 64 * 1024  # 64 KB


class ConnectionClosed(Exception):
    """Raised when the other peer closes the connection (or it breaks)."""


class ProtocolError(Exception):
    """Raised when we receive data that does not follow our protocol."""


# ---------------------------------------------------------------------------
# Low-level helper: read EXACTLY n bytes
# ---------------------------------------------------------------------------
def recv_exact(sock, n):
    """
    Read exactly `n` bytes from `sock`.

    A single sock.recv(n) may return FEWER than n bytes (TCP gives us whatever
    has arrived so far). So we loop until we have all n bytes.
    If recv() returns b"" the other side closed the connection.
    """
    data = bytearray()
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
        except OSError as e:
            raise ConnectionClosed(f"Connection error: {e}") from e
        if not chunk:  # b"" means the peer closed the connection
            raise ConnectionClosed("Peer closed the connection")
        data.extend(chunk)
    return bytes(data)


# ---------------------------------------------------------------------------
# Sending / receiving one framed JSON message
# ---------------------------------------------------------------------------
def send_message(sock, message):
    """Convert a dict to JSON and send it as [4-byte length][JSON bytes]."""
    payload = json.dumps(message).encode("utf-8")
    header = struct.pack(HEADER_FORMAT, len(payload))
    try:
        # sendall() keeps sending until EVERYTHING is sent (send() might not)
        sock.sendall(header + payload)
    except OSError as e:
        raise ConnectionClosed(f"Send failed: {e}") from e


def recv_message(sock):
    """Read one framed JSON message and return it as a dict."""
    header = recv_exact(sock, HEADER_SIZE)
    (length,) = struct.unpack(HEADER_FORMAT, header)

    if length == 0 or length > MAX_JSON_SIZE:
        raise ProtocolError(f"Invalid message length: {length}")

    payload = recv_exact(sock, length)
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError(f"Invalid JSON received: {e}") from e

    if not isinstance(message, dict) or "type" not in message:
        raise ProtocolError("Message must be a JSON object with a 'type' field")
    return message


# ---------------------------------------------------------------------------
# Message builders (create the dicts described in the assignment)
# ---------------------------------------------------------------------------
def make_hello(peer_id, peer_name, port):
    return {"type": MSG_HELLO, "peer_id": peer_id,
            "peer_name": peer_name, "port": port}


def make_hello_ack(peer_id, peer_name, port):
    return {"type": MSG_HELLO_ACK, "peer_id": peer_id,
            "peer_name": peer_name, "port": port}


def make_text(sender_id, sender_name, message):
    return {"type": MSG_TEXT, "sender_id": sender_id,
            "sender_name": sender_name, "message": message}


def make_file_header(sender_id, sender_name, filename, filesize):
    return {"type": MSG_FILE, "sender_id": sender_id,
            "sender_name": sender_name,
            "filename": filename, "filesize": filesize}


def make_error(reason):
    return {"type": MSG_ERROR, "reason": reason}
