"""Tests für Auslesen/Steuern gegen einen nachgebauten WebSocket-Server und ein Fake-Backend."""

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kenwood_setup as ks  # noqa: E402
import kenwood_control as kc  # noqa: E402

MAC = "AABBCCDDEEFF"
APP_URI = "/api/v1/users/42/appliances/abc-123"
GUID = " wsmagic 258EAFA5-E914-47DA-95CA-C5AB0DC85B11".strip()
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_server(messages):
    """Minimaler WS-Server: Handshake + sendet die gegebenen Text-Nachrichten (unmaskiert)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        key = ""
        for line in req.decode().split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
        conn.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode()
        )
        for msg in messages:
            payload = msg.encode()
            header = bytes([0x81])
            if len(payload) < 126:
                header += bytes([len(payload)])
            else:
                header += bytes([126]) + struct.pack(">H", len(payload))
            conn.sendall(header + payload)
        conn.sendall(bytes([0x88, 0x00]))  # close
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


class FakeBackend(BaseHTTPRequestHandler):
    posts = []
    ws_port = None

    def log_message(self, *a):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/v1/users/42/appliances":
            return self._json(200, {"results": [{"uri": APP_URI, "native_id": MAC, "appliance": {"uri": "/api/v1/appliances/209"}}]})
        if parsed.path == "/api/v1/users/42/events":
            return self._json(200, {"events_url": f"ws://127.0.0.1:{FakeBackend.ws_port}/ws/user/token"})
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        FakeBackend.posts.append((self.path, json.loads(raw or b"{}")))
        self._json(200, {"acknowledged": True})


class ControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeBackend.posts = []
        self.cfg = {"api": self.api, "client_id": "cid", "client_secret": "sec"}
        self.token = {"access_token": "at", "refresh_token": "rt", "user": {"id": 42}}

    def test_parse_event(self):
        self.assertEqual(kc.parse_event({"event": {"type": "Temperature", "value": 27}}), ("Temperature", 27))
        self.assertEqual(kc.parse_event({"event": {"type": "Status", "value": "ready"}}), ("Status", "ready"))
        self.assertEqual(kc.parse_event({"foo": 1}), (None, None))

    def _ev(self, nid, etype, value):
        return json.dumps({"native_id": nid, "step_id": None, "preset_id": None, "event": {"type": etype, "value": value, "timestamp": 1}})

    def test_stream_events_subscribes_and_reads(self):
        FakeBackend.ws_port = ws_server([self._ev(MAC, "Temperature", 20)])
        got = []
        kc.stream_events(self.cfg, self.token, MAC, lambda t, v, r: got.append((t, v)), once=True)
        self.assertEqual(got, [("Temperature", 20)])
        self.assertIn(f"{APP_URI}/subscribe", [p for p, _ in FakeBackend.posts])

    def test_stream_events_filters_other_appliance(self):
        FakeBackend.ws_port = ws_server([self._ev("OTHER", "Status", "cooking"), self._ev(MAC, "Status", "ready")])
        got = []
        kc.stream_events(self.cfg, self.token, MAC, lambda t, v, r: got.append((t, v)), once=True)
        self.assertEqual(got, [("Status", "ready")])

    def test_ws_client_handles_ping(self):
        FakeBackend.ws_port = self._ws_server_with_ping([self._ev(MAC, "Status", "ok")])
        got = []
        kc.stream_events(self.cfg, self.token, MAC, lambda t, v, r: got.append((t, v)), once=True)
        self.assertEqual(got, [("Status", "ok")])

    def _ws_server_with_ping(self, messages):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            conn, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            key = next(l.split(":", 1)[1].strip() for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            conn.sendall(bytes([0x89, 0x00]))  # ping
            # Pong des Clients lesen und verwerfen
            conn.recv(64)
            for msg in messages:
                payload = msg.encode()
                conn.sendall(bytes([0x81, len(payload)]) + payload)
            conn.sendall(bytes([0x88, 0x00])); conn.close(); srv.close()

        threading.Thread(target=serve, daemon=True).start()
        return port

    def test_user_appliance_requires_match(self):
        with self.assertRaises(SystemExit):
            kc.user_appliance(self.cfg, self.token, "UNKNOWNMAC")


if __name__ == "__main__":
    unittest.main()
