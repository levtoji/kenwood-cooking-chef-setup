#!/usr/bin/env python3
"""Cooking Chef XL vom Mac aus auslesen und steuern – über die Drop/Kenwood-Cloud-API.

Protokoll (aus der Kenwood-World-App):
  Auslesen (live):  GET  {userUri}/events?client_id=…&type=ws  -> {"events_url": "wss://…"}
                    danach WebSocket verbinden, JSON-Events empfangen
  Abonnieren:       POST {userApplianceUri}/subscribe
  Einstellen:       POST {userApplianceUri}/features  [{"key": "temperature", "value": "80"}, …]
  Bedienen:         POST /api/v1/users/{id}/appliances/{applianceId}/operate  {"command": …, "value": …}

Nur Standardbibliothek. Auth/HTTP/Token kommen aus kenwood_setup.
"""

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse

import kenwood_setup as ks

COOKING_CHEF_APPLIANCE_URI = ks.COOKING_CHEF_APPLIANCE_URI


# --- Konto / Gerät ---------------------------------------------------------


def user_appliance(cfg, token, native_id=None):
    """Liefert (user_uri, appliance_uri, native_id) für die Cooking Chef am Konto."""
    uid = token["user"]["id"]
    user_uri = f"/api/v1/users/{uid}"
    status, body = ks.http("GET", f"{cfg['api']}{user_uri}/appliances", headers=ks.auth_header(token))
    if status != 200 or not isinstance(body, dict):
        sys.exit(f"Konnte Geräte nicht laden ({status}): {body}")
    matches = [
        r for r in body.get("results", [])
        if r.get("appliance", {}).get("uri") == COOKING_CHEF_APPLIANCE_URI
        and (native_id is None or r.get("native_id") == native_id)
    ]
    if not matches:
        sys.exit("Keine Cooking Chef XL am Konto gefunden. Erst 'kenwood_setup.py setup' ausführen.")
    if len(matches) > 1:
        ids = ", ".join(r["native_id"] for r in matches)
        sys.exit(f"Mehrere Geräte gefunden ({ids}) – bitte --native-id angeben.")
    return user_uri, matches[0]["uri"], matches[0]["native_id"]


def subscribe(cfg, token, appliance_uri):
    ks.http("POST", f"{cfg['api']}{appliance_uri}/subscribe", data={}, headers=ks.auth_header(token))


def events_url(cfg, token, user_uri):
    query = urllib.parse.urlencode({"client_id": cfg["client_id"], "type": "ws"})
    status, body = ks.http("GET", f"{cfg['api']}{user_uri}/events?{query}", headers=ks.auth_header(token))
    if status != 200 or not isinstance(body, dict) or "events_url" not in body:
        sys.exit(f"Konnte Events-URL nicht holen ({status}): {body}")
    return body["events_url"]


# --- Minimaler WebSocket-Client (RFC 6455, nur Empfang) --------------------


class WebSocket:
    """Gerade genug WebSocket, um Text-Frames zu empfangen (Client, TLS)."""

    def __init__(self, url, timeout=70):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        raw = socket.create_connection((host, port), timeout=timeout)
        if parts.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        self._buf = b""
        header = self._read_until(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket-Handshake fehlgeschlagen: {header[:120]!r}")

    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Verbindung vor Handshake-Ende geschlossen")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _recv_exactly(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket geschlossen")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _send_frame(self, opcode, payload=b""):
        header = bytes([0x80 | opcode])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self):
        """Gibt den nächsten Text-Frame als str zurück (oder None bei Close)."""
        while True:
            b0, b1 = self._recv_exactly(2)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exactly(8))[0]
            payload = self._recv_exactly(length) if length else b""
            if opcode == 0x8:  # close
                return None
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x2):  # text / binary
                return payload.decode("utf-8", "replace")
            # andere Frames (pong, continuation) ignorieren

    def close(self):
        try:
            self._send_frame(0x8)
        except OSError:
            pass
        self.sock.close()


# --- Event-Auswertung ------------------------------------------------------
# Reales Event-Format der Maschine:
#   {"native_id": "...", "step_id": null, "preset_id": null,
#    "event": {"type": "Temperature", "value": 27, "timestamp": 169…}}


def parse_event(message):
    """Gibt (type, value) aus einem Event zurück, oder (None, None)."""
    event = message.get("event") if isinstance(message, dict) else None
    if isinstance(event, dict) and "type" in event:
        return event.get("type"), event.get("value")
    return None, None


def stream_events(cfg, token, native_id, on_event, once=False, timeout=70):
    """Ruft on_event(type, value, raw_message) für jedes Event der Maschine auf."""
    user_uri, appliance_uri, native_id = user_appliance(cfg, token, native_id)
    subscribe(cfg, token, appliance_uri)
    ws = WebSocket(events_url(cfg, token, user_uri), timeout=timeout)
    try:
        while True:
            raw = ws.recv()
            if raw is None:
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if native_id and message.get("native_id") != native_id:
                continue
            etype, value = parse_event(message)
            if etype is None:
                continue
            on_event(etype, value, message)
            if once:
                return
    finally:
        ws.close()


# --- Steuern ---------------------------------------------------------------


def set_features(cfg, token, appliance_uri, features):
    body = [{"key": k, "value": str(v)} for k, v in features.items()]
    status, resp = ks.http("POST", f"{cfg['api']}{appliance_uri}/features", data=body, headers=ks.auth_header(token))
    print(f"  features: {status} {resp}")
    return status == 200


def operate(cfg, token, user_uri, appliance_id, command, value):
    uid = user_uri.rsplit("/", 1)[-1]
    url = f"{cfg['api']}/api/v1/users/{uid}/appliances/{appliance_id}/operate"
    status, resp = ks.http("POST", url, data={"command": command, "value": value}, headers=ks.auth_header(token))
    print(f"  operate: {status} {resp}")
    return status == 200


# --- Befehle ---------------------------------------------------------------


def cmd_status(args):
    """Sammelt einige Sekunden Events zu einem Zustands-Schnappschuss."""
    cfg = ks.load_env()
    token = ks.get_token(cfg)
    state = {}
    deadline = time.time() + args.seconds
    def collect(etype, value, _raw):
        state[etype] = value
        if time.time() >= deadline:
            raise _Done()
    try:
        stream_events(cfg, token, args.native_id, collect, once=False, timeout=args.seconds + 5)
    except _Done:
        pass
    print(json.dumps(state, ensure_ascii=False, indent=2))


def cmd_watch(args):
    cfg = ks.load_env()
    token = ks.get_token(cfg)
    def show(etype, value, _raw):
        print(f"[{time.strftime('%H:%M:%S')}] {etype} = {value}")
    print("Lausche auf Live-Events (Strg+C beendet) …")
    stream_events(cfg, token, args.native_id, show, once=False, timeout=args.timeout)


class _Done(Exception):
    pass


def cmd_set(args):
    cfg = ks.load_env()
    token = ks.get_token(cfg)
    features = {k: v for k, v in (("temperature", args.temperature), ("speed", args.speed), ("time", args.time), ("power", args.power)) if v is not None}
    if not features:
        sys.exit("Nichts zu setzen – mindestens eins von --temperature/--speed/--time/--power angeben.")
    _, appliance_uri, _ = user_appliance(cfg, token, args.native_id)
    if not set_features(cfg, token, appliance_uri, features):
        sys.exit("Setzen fehlgeschlagen.")
    print("Gesendet.")


def cmd_operate(args):
    cfg = ks.load_env()
    token = ks.get_token(cfg)
    user_uri, appliance_uri, _ = user_appliance(cfg, token, args.native_id)
    appliance_id = appliance_uri.rsplit("/", 1)[-1]
    if not operate(cfg, token, user_uri, appliance_id, args.command, args.value):
        sys.exit("Befehl fehlgeschlagen.")
    print("Gesendet.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--native-id", help="MAC/native_id, falls mehrere Geräte am Konto hängen")
    parser.add_argument("--timeout", type=int, default=70, help="Sekunden Wartezeit auf WebSocket-Events")
    sub = parser.add_subparsers(required=True)

    p_status = sub.add_parser("status", help="Aktuellen Zustand als Schnappschuss ausgeben")
    p_status.add_argument("--seconds", type=int, default=8, help="Sammeldauer für den Schnappschuss")
    p_status.set_defaults(func=cmd_status)
    sub.add_parser("watch", help="Live-Zustand fortlaufend ausgeben").set_defaults(func=cmd_watch)

    p_set = sub.add_parser("set", help="Temperatur/Stufe/Zeit/Leistung setzen")
    p_set.add_argument("--temperature", type=int, help="Zieltemperatur in °C")
    p_set.add_argument("--speed", type=int, help="Rührstufe")
    p_set.add_argument("--time", type=int, help="Zeit in Sekunden")
    p_set.add_argument("--power", help="Leistung/an-aus (gerätespezifisch)")
    p_set.set_defaults(func=cmd_set)

    p_op = sub.add_parser("operate", help="Rohen Befehl senden (command/value)")
    p_op.add_argument("--command", required=True)
    p_op.add_argument("--value", required=True)
    p_op.set_defaults(func=cmd_operate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
