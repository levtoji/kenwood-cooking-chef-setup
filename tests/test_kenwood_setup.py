"""End-to-End-Tests gegen eine simulierte Maschine (HTTPS, selbstsigniert) und ein simuliertes Backend.

WLAN-Wechsel und Schlüsselbund werden ersetzt, damit die Tests ohne echte Hardware laufen.
"""

import io
import json
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kenwood_setup as k  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
HOME_SSID = "HOME_24"
HOME_PASSWORD = "geheim123"


class FakeAppliance(BaseHTTPRequestHandler):
    joins = []
    networks = [{"ssid": HOME_SSID, "rssi": -50, "security": "wpa2"}, {"ssid": "Nachbar", "rssi": -80, "security": "open"}]

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/mac":
            return self._json(200, {"mac": MAC})
        if self.path == "/networks":
            return self._json(200, {"networks": self.networks})
        self._json(404, {})

    def do_POST(self):
        if self.path == "/join":
            length = int(self.headers["Content-Length"])
            FakeAppliance.joins.append(json.loads(self.rfile.read(length)))
            return self._json(200, {"status": "ok"})
        self._json(404, {})


class FakeBackend(BaseHTTPRequestHandler):
    token_requests = []
    connected_polls = []
    reject_first_token = False

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/v1/organizations/2/auth/token":
            length = int(self.headers["Content-Length"])
            form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
            FakeBackend.token_requests.append(form)
            if form.get("client_id") != "cid":
                return self._json(401, {"error": "invalid_client"})
            if form["grant_type"] == "password" and form.get("password") != "kontopw":
                return self._json(401, {"error": "invalid_grant"})
            return self._json(200, {"access_token": f"at{len(FakeBackend.token_requests)}", "refresh_token": "rt", "user": {"id": 42}})
        self._json(404, {})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/v1/appliances/209/connected":
            FakeBackend.connected_polls.append((urllib.parse.parse_qs(parsed.query), self.headers.get("Authorization")))
            if FakeBackend.reject_first_token and len(FakeBackend.connected_polls) == 1:
                return self._json(401, {"name": "Missing token"})
            state = "connected" if len(FakeBackend.connected_polls) >= 2 else "connecting"
            return self._json(200, {"connection_state": state})
        self._json(404, {})


def start(handler, tls_dir=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    scheme = "http"
    if tls_dir:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_dir / "cert.pem", tls_dir / "key.pem")
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"{scheme}://127.0.0.1:{server.server_address[1]}"


class SetupFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=192.168.4.1",
             "-keyout", cls.tmp / "key.pem", "-out", cls.tmp / "cert.pem"],
            check=True, capture_output=True,
        )
        cls.appliance, cls.appliance_url = start(FakeAppliance, cls.tmp)
        cls.backend, cls.backend_url = start(FakeBackend)

    @classmethod
    def tearDownClass(cls):
        cls.appliance.shutdown()
        cls.backend.shutdown()

    def setUp(self):
        FakeAppliance.joins = []
        FakeBackend.token_requests = []
        FakeBackend.connected_polls = []
        FakeBackend.reject_first_token = False
        self.wifi_calls = []
        token_file = self.tmp / "token.json"
        token_file.unlink(missing_ok=True)
        env = {"KW_CLIENT_ID": "cid", "KW_CLIENT_SECRET": "secret", "KW_API_BASE_URL": self.backend_url}
        self.patches = [
            mock.patch.dict("os.environ", env),
            mock.patch.object(k, "ENV_FILE", self.tmp / "missing.env"),
            mock.patch.object(k, "TOKEN_FILE", token_file),
            mock.patch.object(k, "APPLIANCE_HOST", self.appliance_url),
            mock.patch.object(k, "join_wifi", side_effect=lambda ssid, pw=None: self.wifi_calls.append((ssid, pw)) or True),
            mock.patch.object(k, "keychain_password", side_effect=lambda ssid: HOME_PASSWORD if ssid == HOME_SSID else None),
            mock.patch.object(k.time, "sleep", lambda s: None),
            mock.patch("getpass.getpass", return_value="kontopw"),
            mock.patch("builtins.input", return_value=""),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["kenwood_setup.py", *argv]), redirect_stdout(out):
            try:
                k.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_login_stores_token(self):
        code, out = self.run_cli("login", "--email", "me@example.com")
        self.assertEqual(code, 0, out)
        self.assertIn("Login ok (User-ID 42)", out)
        self.assertEqual(FakeBackend.token_requests[0]["username"], "me@example.com")
        self.assertEqual(FakeBackend.token_requests[0]["client_secret"], "secret")
        self.assertEqual(oct(k.TOKEN_FILE.stat().st_mode & 0o777), "0o600")

    def test_login_wrong_password_fails(self):
        with mock.patch("getpass.getpass", return_value="falsch"):
            code, _ = self.run_cli("login")
        self.assertIn("Login fehlgeschlagen (401)", str(code))

    def test_login_requires_tty(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, _ = self.run_cli("login")
        self.assertIn("interaktives Terminal", str(code))

    def test_setup_full_flow(self):
        # Vor dem WLAN-Wechsel ist die Maschine nicht erreichbar, danach schon.
        with mock.patch.object(k, "wait_for_appliance", side_effect=[None, MAC]):
            code, out = self.run_cli("setup", "--ap", "KENWOOD1234", "--ssid", HOME_SSID)
        self.assertEqual(code, 0, out)
        self.assertEqual(FakeAppliance.joins, [{"ssid": HOME_SSID, "password": HOME_PASSWORD}])
        self.assertEqual(self.wifi_calls, [("KENWOOD1234", None), (HOME_SSID, HOME_PASSWORD)])
        query, auth = FakeBackend.connected_polls[0]
        self.assertEqual(query, {"native_id": [MAC]})
        self.assertTrue(auth.startswith("Bearer at"))
        self.assertIn("Fertig", out)

    def test_setup_detects_ap_and_home_ssid_from_env(self):
        with mock.patch.object(k, "wait_for_appliance", side_effect=[None, MAC]), \
                mock.patch.object(k, "find_setup_ap", return_value="KENWOOD9999"), \
                mock.patch.dict("os.environ", {"KW_HOME_SSID": HOME_SSID}):
            code, out = self.run_cli("setup")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.wifi_calls, [("KENWOOD9999", None), (HOME_SSID, HOME_PASSWORD)])

    def test_setup_skips_wifi_switch_when_already_on_ap(self):
        with mock.patch.object(k, "wait_for_appliance", return_value=MAC):
            code, out = self.run_cli("setup", "--ap", "KENWOOD1234", "--ssid", HOME_SSID)
        self.assertEqual(code, 0, out)
        self.assertIn("bereits im Setup-WLAN", out)
        self.assertEqual(self.wifi_calls, [(HOME_SSID, HOME_PASSWORD)])

    def test_setup_reuses_stored_token_via_refresh(self):
        self.run_cli("login")
        with mock.patch("getpass.getpass", side_effect=AssertionError("kein Passwort erwartet")):
            code, out = self.run_cli("setup", "--ap", "KENWOOD1234", "--ssid", HOME_SSID)
        self.assertEqual(code, 0, out)
        self.assertEqual(FakeBackend.token_requests[-1]["grant_type"], "refresh_token")

    def test_setup_relogs_on_401(self):
        FakeBackend.reject_first_token = True
        code, out = self.run_cli("setup", "--ap", "KENWOOD1234", "--ssid", HOME_SSID, "--timeout", "1000")
        self.assertEqual(code, 0, out)
        first_auth, second_auth = FakeBackend.connected_polls[0][1], FakeBackend.connected_polls[1][1]
        self.assertNotEqual(first_auth, second_auth)
        self.assertEqual(FakeBackend.token_requests[-1]["grant_type"], "refresh_token")

    def test_setup_aborts_when_ssid_not_visible_and_returns_home(self):
        code, _ = self.run_cli("setup", "--ap", "KENWOOD1234", "--ssid", "NUR_5GHZ", "--return-ssid", HOME_SSID)
        self.assertIn("sieht NUR_5GHZ nicht", str(code))
        self.assertEqual(FakeAppliance.joins, [])
        self.assertEqual(self.wifi_calls[-1], (HOME_SSID, HOME_PASSWORD))

    def test_probe(self):
        code, out = self.run_cli("probe")
        self.assertEqual(code, 0)
        self.assertIn(MAC, out)
        self.assertIn(HOME_SSID, out)


if __name__ == "__main__":
    unittest.main()
