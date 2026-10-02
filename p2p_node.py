"""
p2p_node.py
===========
The P2P networking layer.

    Every peer = TCP Server + TCP Client

* SERVER role : a listening socket accepts connections from other peers.
* CLIENT role : connect_to_peer() opens a connection to another peer.
* THREADS     : every connection gets its own thread that keeps reading
                incoming messages, so we can talk to many peers at once.

This file knows NOTHING about the GUI. It reports what happens through a
callback function (`on_event`), and the GUI decides how to display it.
That separation keeps the networking code simple and testable.

Events sent to the callback: on_event(kind, data_dict)
    kind = "log"            data = {"text": "..."}
    kind = "error"          data = {"text": "..."}
    kind = "peers_changed"  data = {}
    kind = "text"           data = {"peer_id", "peer_name", "message"}
    kind = "file_received"  data = {"peer_name", "filename", "path", "size"}
"""

import os
import socket
import threading
import uuid

import protocol
from protocol import ConnectionClosed, ProtocolError

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


class PeerConnection:
    """Everything we know about ONE connected remote peer."""

    def __init__(self, sock, address, peer_id, peer_name, listen_port):
        self.sock = sock                # the TCP socket to this peer
        self.address = address          # (ip, port) as seen by our socket
        self.peer_id = peer_id          # unique id, e.g. "a83f21c4"
        self.peer_name = peer_name      # e.g. "Bob"
        self.listen_port = listen_port  # the port that peer is LISTENING on
        # Two of our threads might send on the same socket at once (e.g. the
        # user sends a file while a reply is sent). The lock makes sure two
        # messages never get mixed up byte-by-byte.
        self.send_lock = threading.Lock()

    def display(self):
        return f"{self.peer_name} [{self.peer_id}]"


class P2PNode:
    def __init__(self, name, port, on_event=None):
        self.name = name
        self.port = port
        self.peer_id = uuid.uuid4().hex[:8]     # short random id like "a83f21c4"
        self.on_event = on_event or (lambda kind, data: None)

        self.server_socket = None
        self.running = False

        self.peers = {}                          # peer_id -> PeerConnection
        self.peers_lock = threading.Lock()       # protects self.peers

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # small helpers to talk to the GUI
    # ------------------------------------------------------------------
    def _log(self, text):
        self.on_event("log", {"text": text})

    def _error(self, text):
        self.on_event("error", {"text": text})

    # ==================================================================
    # SERVER ROLE
    # ==================================================================
    def start(self):
        """Create the listening socket and start accepting peers."""
        # Step 1 of the assignment: socket() -> bind() -> listen()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Lets us restart quickly on the same port without "address in use"
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # "0.0.0.0" = listen on ALL network interfaces (needed so other
            # computers on the LAN can reach us, not only 127.0.0.1)
            self.server_socket.bind(("0.0.0.0", self.port))
            self.server_socket.listen()
        except OSError as e:
            self.server_socket.close()
            self.server_socket = None
            raise OSError(f"Cannot listen on port {self.port}: {e}") from e

        self.running = True
        # The accept loop runs in a background (daemon) thread so the GUI
        # stays responsive. daemon=True => thread dies when the program exits.
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self._log(f"Peer '{self.name}' [{self.peer_id}] listening on port {self.port}")

    def _accept_loop(self):
        """Wait for incoming connections; give each one its own thread."""
        while self.running:
            try:
                # accept() BLOCKS until some peer connects to us.
                conn, addr = self.server_socket.accept()
            except OSError:
                break  # server socket was closed by stop()
            threading.Thread(target=self._handle_incoming,
                             args=(conn, addr), daemon=True).start()

    def _handle_incoming(self, conn, addr):
        """
        We are the SERVER side of this connection.
        Handshake: wait for the other side's HELLO, reply with HELLO_ACK.
        """
        try:
            conn.settimeout(10)  # don't wait forever for a HELLO
            hello = protocol.recv_message(conn)
            if hello["type"] != protocol.MSG_HELLO:
                raise ProtocolError("Expected 'hello' as first message")
            conn.settimeout(None)

            peer_id = hello["peer_id"]
            # Refuse (and TELL the other side why) instead of silently hanging up
            refusal = None
            if peer_id == self.peer_id:
                refusal = "You tried to connect to yourself"
            elif peer_id in self.peers:
                refusal = "Already connected to this peer"
            if refusal:
                try:
                    protocol.send_message(conn, protocol.make_error(refusal))
                except ConnectionClosed:
                    pass
                raise ProtocolError(refusal)

            protocol.send_message(
                conn, protocol.make_hello_ack(self.peer_id, self.name, self.port))

            peer = PeerConnection(conn, addr, peer_id,
                                  hello["peer_name"], hello["port"])
            self._register_peer(peer)
        except (ConnectionClosed, ProtocolError, KeyError, OSError) as e:
            self._error(f"Incoming connection from {addr[0]} rejected: {e}")
            self._close_socket(conn)
            return

        self._receive_loop(peer)

    # ==================================================================
    # CLIENT ROLE
    # ==================================================================
    def connect_to_peer(self, ip, port):
        """
        Connect to another peer at ip:port (we are the CLIENT side).
        Handshake: send HELLO, wait for HELLO_ACK.
        Called from the GUI; runs the network work in a background thread so
        the window never freezes while connecting.
        """
        threading.Thread(target=self._connect_worker,
                         args=(ip, port), daemon=True).start()

    def _connect_worker(self, ip, port):
        if not self.running:
            self._error("Start your peer first.")
            return
        self._log(f"Connecting to {ip}:{port} ...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(5)                  # give up after 5 seconds
            sock.connect((ip, port))            # <-- TCP connection made here
            protocol.send_message(
                sock, protocol.make_hello(self.peer_id, self.name, self.port))
            ack = protocol.recv_message(sock)
            if ack["type"] == protocol.MSG_ERROR:       # the other peer refused us
                raise ProtocolError(f"Refused by peer: {ack.get('reason', 'unknown reason')}")
            if ack["type"] != protocol.MSG_HELLO_ACK:
                raise ProtocolError("Expected 'hello_ack' reply")
            sock.settimeout(None)

            peer_id = ack["peer_id"]
            if peer_id == self.peer_id:
                raise ProtocolError("You tried to connect to yourself")
            if peer_id in self.peers:
                raise ProtocolError("Already connected to this peer")

            peer = PeerConnection(sock, (ip, port), peer_id,
                                  ack["peer_name"], ack["port"])
        except socket.timeout:
            self._error(f"Connection failed: timed out connecting to {ip}:{port}")
            self._close_socket(sock)
            return
        except (ConnectionClosed, ProtocolError, KeyError, OSError) as e:
            self._error(f"Connection failed: {e}")
            self._close_socket(sock)
            return

        self._register_peer(peer)
        self._receive_loop(peer)   # this thread now serves this connection

    # ==================================================================
    # Managing the peer list
    # ==================================================================
    def _register_peer(self, peer):
        with self.peers_lock:
            self.peers[peer.peer_id] = peer
        self._log(f"Connected to {peer.display()} ({peer.address[0]})")
        self.on_event("peers_changed", {})

    def _remove_peer(self, peer, reason):
        with self.peers_lock:
            removed = self.peers.pop(peer.peer_id, None)
        self._close_socket(peer.sock)
        if removed:
            self._error(f"{peer.display()} disconnected ({reason})")
            self.on_event("peers_changed", {})

    def get_peers(self):
        """Return a snapshot list of connected peers (safe to iterate)."""
        with self.peers_lock:
            return list(self.peers.values())

    @staticmethod
    def _close_socket(sock):
        """
        Fully close a socket.
        shutdown() FIRST: if another thread is blocked in recv() on this
        socket, a plain close() would NOT end the TCP connection, and the
        remote peer would never notice we left. shutdown() sends the TCP FIN
        and wakes up every thread using the socket.
        """
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass   # already closed / never connected - that's fine
        try:
            sock.close()
        except OSError:
            pass

    # ==================================================================
    # RECEIVING (one thread per connected peer runs this loop)
    # ==================================================================
    def _receive_loop(self, peer):
        """Keep reading messages from `peer` until it disconnects."""
        try:
            while self.running:
                msg = protocol.recv_message(peer.sock)
                mtype = msg["type"]

                if mtype == protocol.MSG_TEXT:
                    self.on_event("text", {
                        "peer_id": peer.peer_id,
                        "peer_name": peer.peer_name,
                        "message": str(msg.get("message", "")),
                    })
                elif mtype == protocol.MSG_FILE:
                    self._receive_file(peer, msg)
                else:
                    self._log(f"Ignoring unknown message type '{mtype}' "
                              f"from {peer.display()}")
        except (ConnectionClosed, ProtocolError) as e:
            if self.running:
                self._remove_peer(peer, str(e))
        except Exception as e:  # never let a thread die silently
            if self.running:
                self._remove_peer(peer, f"unexpected error: {e}")

    # ==================================================================
    # TEXT
    # ==================================================================
    def send_text(self, peer_id, text):
        """Send a text message to one specific peer. Returns True on success."""
        peer = self.peers.get(peer_id)
        if peer is None:
            self._error("That peer is no longer connected.")
            return False
        try:
            with peer.send_lock:
                protocol.send_message(
                    peer.sock, protocol.make_text(self.peer_id, self.name, text))
            return True
        except ConnectionClosed as e:
            self._remove_peer(peer, str(e))
            return False

    # ==================================================================
    # FILE TRANSFER
    # ==================================================================
    def send_file(self, peer_id, filepath):
        """
        Send a file to one peer. Runs in a background thread so a big video
        does not freeze the GUI.
        """
        # Validate BEFORE starting the thread so errors show immediately
        if not os.path.isfile(filepath):
            self._error(f"File does not exist: {filepath}")
            return
        peer = self.peers.get(peer_id)
        if peer is None:
            self._error("That peer is no longer connected.")
            return
        threading.Thread(target=self._send_file_worker,
                         args=(peer, filepath), daemon=True).start()

    def _send_file_worker(self, peer, filepath):
        filename = os.path.basename(filepath)
        try:
            filesize = os.path.getsize(filepath)
            self._log(f"Sending '{filename}' ({filesize} bytes) to {peer.display()} ...")

            # The lock is held for the WHOLE transfer so no other message
            # (e.g. a text) can be squeezed into the middle of the file bytes.
            with peer.send_lock:
                # Stage 1: metadata (a normal framed JSON message)
                protocol.send_message(peer.sock, protocol.make_file_header(
                    self.peer_id, self.name, filename, filesize))
                # Stage 2: raw bytes, in chunks (never the whole file in RAM)
                sent = 0
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(protocol.CHUNK_SIZE)
                        if not chunk:
                            break
                        peer.sock.sendall(chunk)
                        sent += len(chunk)
            self._log(f"Sent '{filename}' to {peer.display()} "
                      f"({sent} bytes) - done.")
        except OSError as e:
            # covers both file-read errors and socket errors
            self._error(f"File transfer failed: {e}")
            self._remove_peer(peer, "connection lost during file transfer")
        except ConnectionClosed as e:
            self._error(f"File transfer failed: {e}")
            self._remove_peer(peer, str(e))

    def _receive_file(self, peer, header):
        """
        The header (JSON) has just arrived. Now read EXACTLY `filesize` raw
        bytes from the socket and write them to downloads/<filename>.
        """
        filename = header.get("filename")
        filesize = header.get("filesize")

        # ---- validate the metadata (never trust data from the network) ----
        if not isinstance(filename, str) or not isinstance(filesize, int) or filesize < 0:
            raise ProtocolError("Invalid file metadata (bad filename or filesize)")

        # SECURITY: strip any folder part so "../../evil.py" can't escape
        # the downloads folder. Only the plain file name is kept.
        safe_name = os.path.basename(filename.replace("\\", "/"))
        if not safe_name:
            raise ProtocolError("Invalid file name")

        save_path = self._unique_path(os.path.join(DOWNLOAD_DIR, safe_name))
        self._log(f"Receiving '{safe_name}' ({filesize} bytes) from {peer.display()} ...")

        remaining = filesize
        try:
            with open(save_path, "wb") as f:
                while remaining > 0:
                    # never read more than what belongs to THIS file
                    chunk = peer.sock.recv(min(protocol.CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ConnectionClosed("Peer disconnected during file transfer")
                    f.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionClosed, OSError):
            # incomplete file -> delete it, then let the receive loop
            # handle the disconnect
            try:
                os.remove(save_path)
            except OSError:
                pass
            raise ConnectionClosed("Peer disconnected during file transfer")

        self.on_event("file_received", {
            "peer_name": peer.peer_name, "filename": os.path.basename(save_path),
            "path": save_path, "size": filesize})

    @staticmethod
    def _unique_path(path):
        """If photo.jpg exists, use photo (1).jpg, photo (2).jpg, ..."""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        return f"{base} ({i}){ext}"

    # ==================================================================
    # STOP
    # ==================================================================
    def stop(self):
        """Close the server socket and every peer connection."""
        self.running = False
        if self.server_socket:
            self._close_socket(self.server_socket)   # unblocks accept()
            self.server_socket = None
        with self.peers_lock:
            peers = list(self.peers.values())
            self.peers.clear()
        for p in peers:
            self._close_socket(p.sock)
        self.on_event("peers_changed", {})
        self._log("Peer stopped.")
