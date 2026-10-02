"""
main.py
=======
User interface (Tkinter) for the P2P chat + file sharing application.

Run with:   python main.py

IMPORTANT CONCEPT - THREADS AND THE GUI
---------------------------------------
Tkinter is NOT thread-safe: only the main thread may touch widgets.
But our networking runs in many background threads. So:

    network threads --(put events)--> queue.Queue --(GUI polls)--> widgets

The networking code calls `_on_network_event()`, which only puts the event in
a queue. `_process_events()` runs on the GUI thread every 100 ms (via
root.after) and takes events out of the queue and updates the screen.
"""

import os
import queue
import socket
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from p2p_node import P2PNode


def get_local_ip():
    """
    Best-effort guess of this computer's LAN IP (e.g. 192.168.1.10), so the
    user can tell friends what to type. No data is actually sent: connecting
    a UDP socket just makes the OS pick the outgoing network interface.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class P2PApp:
    def __init__(self, root):
        self.root = root
        self.root.title("P2P Chat & File Sharing")
        self.root.geometry("820x640")
        self.root.minsize(700, 560)

        self.node = None                    # created when "Start Peer" is pressed
        self.events = queue.Queue()         # network threads -> GUI thread
        self.peer_ids = []                  # peer_ids in the same order as the listbox

        self._build_ui()
        self._set_connected_state(False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._process_events)

    # ------------------------------------------------------------------
    # Building the window
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ---- Section 1: My Peer ----
        f1 = ttk.LabelFrame(self.root, text="My Peer")
        f1.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(f1, text="Name:").grid(row=0, column=0, **pad)
        self.name_var = tk.StringVar(value="Alice")
        self.name_entry = ttk.Entry(f1, textvariable=self.name_var, width=16)
        self.name_entry.grid(row=0, column=1, **pad)
        ttk.Label(f1, text="Port:").grid(row=0, column=2, **pad)
        self.port_var = tk.StringVar(value="5000")
        self.port_entry = ttk.Entry(f1, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=3, **pad)
        self.start_btn = ttk.Button(f1, text="Start Peer", command=self._start_peer)
        self.start_btn.grid(row=0, column=4, **pad)
        self.stop_btn = ttk.Button(f1, text="Stop", command=self._stop_peer)
        self.stop_btn.grid(row=0, column=5, **pad)
        self.my_info_var = tk.StringVar(value="Not running")
        ttk.Label(f1, textvariable=self.my_info_var, foreground="#555").grid(
            row=1, column=0, columnspan=6, sticky="w", **pad)

        # ---- Section 2: Connect to another peer ----
        f2 = ttk.LabelFrame(self.root, text="Connect to Another Peer")
        f2.pack(fill="x", padx=10, pady=4)
        ttk.Label(f2, text="IP:").grid(row=0, column=0, **pad)
        self.ip_var = tk.StringVar(value="127.0.0.1")
        self.ip_entry = ttk.Entry(f2, textvariable=self.ip_var, width=18)
        self.ip_entry.grid(row=0, column=1, **pad)
        ttk.Label(f2, text="Port:").grid(row=0, column=2, **pad)
        self.rport_var = tk.StringVar(value="5001")
        self.rport_entry = ttk.Entry(f2, textvariable=self.rport_var, width=8)
        self.rport_entry.grid(row=0, column=3, **pad)
        self.connect_btn = ttk.Button(f2, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=4, **pad)

        # ---- Section 3: peer list (left) + event log (right) ----
        f3 = ttk.Frame(self.root)
        f3.pack(fill="both", expand=True, padx=10, pady=4)
        f3.columnconfigure(1, weight=1)
        f3.rowconfigure(0, weight=1)

        peers_frame = ttk.LabelFrame(f3, text="Connected Peers")
        peers_frame.grid(row=0, column=0, sticky="ns", padx=(0, 6))
        self.peer_list = tk.Listbox(peers_frame, width=26, exportselection=False,
                                    activestyle="none")
        self.peer_list.pack(fill="both", expand=True, padx=6, pady=6)
        ttk.Label(peers_frame, text="Select a peer, then send.",
                  foreground="#555").pack(padx=6, pady=(0, 6))

        log_frame = ttk.LabelFrame(f3, text="Messages / Events")
        log_frame.grid(row=0, column=1, sticky="nsew")
        self.log = tk.Text(log_frame, state="disabled", wrap="word", height=10)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=(6, 0), pady=6)
        # colours for the different kinds of log lines
        self.log.tag_configure("info", foreground="#444")
        self.log.tag_configure("error", foreground="#c62828")
        self.log.tag_configure("incoming", foreground="#1565c0")
        self.log.tag_configure("outgoing", foreground="#2e7d32")
        self.log.tag_configure("file", foreground="#6a1b9a")

        # ---- Section 4: send text ----
        f4 = ttk.LabelFrame(self.root, text="Send")
        f4.pack(fill="x", padx=10, pady=4)
        f4.columnconfigure(1, weight=1)
        ttk.Label(f4, text="Text:").grid(row=0, column=0, **pad)
        self.msg_var = tk.StringVar()
        self.msg_entry = ttk.Entry(f4, textvariable=self.msg_var)
        self.msg_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.msg_entry.bind("<Return>", lambda e: self._send_text())
        self.send_btn = ttk.Button(f4, text="Send", command=self._send_text)
        self.send_btn.grid(row=0, column=2, **pad)
        self.file_btn = ttk.Button(f4, text="Choose File & Send",
                                   command=self._send_file)
        self.file_btn.grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        # ---- status bar ----
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    # Enabling / disabling controls
    # ------------------------------------------------------------------
    def _set_connected_state(self, running):
        """running=True  -> peer is started: enable connect/send controls."""
        idle = "disabled" if running else "normal"
        active = "normal" if running else "disabled"
        for w in (self.name_entry, self.port_entry, self.start_btn):
            w.configure(state=idle)
        for w in (self.stop_btn, self.ip_entry, self.rport_entry, self.connect_btn,
                  self.msg_entry, self.send_btn, self.file_btn):
            w.configure(state=active)

    # ------------------------------------------------------------------
    # Log helpers (GUI thread only)
    # ------------------------------------------------------------------
    def _log(self, text, tag="info"):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_peer_list(self):
        selected = self._selected_peer_id()
        self.peer_list.delete(0, "end")
        self.peer_ids = []
        if self.node:
            for p in self.node.get_peers():
                self.peer_list.insert("end", p.display())
                self.peer_ids.append(p.peer_id)
        # keep the selection if that peer is still there
        if selected in self.peer_ids:
            self.peer_list.selection_set(self.peer_ids.index(selected))
        elif len(self.peer_ids) == 1:
            self.peer_list.selection_set(0)     # convenience: auto-select the only peer

    def _selected_peer_id(self):
        sel = self.peer_list.curselection()
        if not sel or sel[0] >= len(self.peer_ids):
            return None
        return self.peer_ids[sel[0]]

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _start_peer(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Invalid name", "Please enter a peer name.")
            return
        try:
            port = int(self.port_var.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number between 1 and 65535.")
            return

        self.node = P2PNode(name, port, on_event=self._on_network_event)
        try:
            self.node.start()
        except OSError as e:
            self.node = None
            self._log(f"[ERROR] {e}", "error")
            messagebox.showerror("Cannot start peer", str(e))
            return

        ip = get_local_ip()
        self.my_info_var.set(f"{name} [{self.node.peer_id}]  |  listening on "
                             f"{ip}:{port}  (others connect to this address)")
        self.root.title(f"P2P - {name} (port {port})")
        self.status_var.set(f"Peer running on {ip}:{port}")
        self._set_connected_state(True)

    def _stop_peer(self):
        if self.node:
            self.node.stop()
            self.node = None
        self.my_info_var.set("Not running")
        self.root.title("P2P Chat & File Sharing")
        self.status_var.set("Peer stopped.")
        self._set_connected_state(False)
        self._refresh_peer_list()

    def _connect(self):
        ip = self.ip_var.get().strip()
        try:
            socket.inet_aton(ip)               # validates the IPv4 format
            if ip.count(".") != 3:
                raise OSError
        except OSError:
            self._log(f"[ERROR] Invalid IP address: '{ip}'", "error")
            messagebox.showerror("Invalid IP", f"'{ip}' is not a valid IPv4 address.")
            return
        try:
            port = int(self.rport_var.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            self._log("[ERROR] Invalid remote port", "error")
            messagebox.showerror("Invalid port", "Port must be a number between 1 and 65535.")
            return
        self.node.connect_to_peer(ip, port)

    def _send_text(self):
        text = self.msg_var.get().strip()
        if not text:
            return
        peer_id = self._selected_peer_id()
        if peer_id is None:
            self._log("[ERROR] Select a peer from the list first.", "error")
            messagebox.showwarning("No peer selected",
                                   "Please select a connected peer from the list first.")
            return
        if self.node.send_text(peer_id, text):
            name = self._peer_name(peer_id)
            self._log(f"You -> {name}: {text}", "outgoing")
            self.msg_var.set("")

    def _send_file(self):
        peer_id = self._selected_peer_id()
        if peer_id is None:
            self._log("[ERROR] Select a peer from the list first.", "error")
            messagebox.showwarning("No peer selected",
                                   "Please select a connected peer from the list first.")
            return
        path = filedialog.askopenfilename(title="Choose a file to send")
        if not path:
            return                              # user cancelled the dialog
        self.node.send_file(peer_id, path)

    def _peer_name(self, peer_id):
        for p in self.node.get_peers():
            if p.peer_id == peer_id:
                return p.peer_name
        return "?"

    def _on_close(self):
        if self.node:
            self.node.stop()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Network events -> GUI
    # ------------------------------------------------------------------
    def _on_network_event(self, kind, data):
        """Called from NETWORK threads. Must NOT touch any widget!"""
        self.events.put((kind, data))

    def _process_events(self):
        """Runs on the GUI thread every 100 ms."""
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._log(data["text"], "info")
                elif kind == "error":
                    self._log("[ERROR] " + data["text"], "error")
                    self.status_var.set(data["text"])
                elif kind == "peers_changed":
                    self._refresh_peer_list()
                elif kind == "text":
                    self._log(f"{data['peer_name']} -> You: {data['message']}", "incoming")
                elif kind == "file_received":
                    self._log(f"File received from {data['peer_name']}: "
                              f"{data['filename']} ({data['size']} bytes) "
                              f"-> saved to {data['path']}", "file")
                    self.status_var.set(f"File received: {data['filename']}")
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)


def main():
    root = tk.Tk()
    P2PApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
