#!/usr/bin/env python3
"""Cooking Chef XL vom Mac aus auslesen und steuern – über die Drop/Kenwood-Cloud-API.

Protokoll (aus der Kenwood-World-App):
  Auslesen (live):  GET  {userUri}/events?client_id=…&type=ws  -> {"events_url": "wss://…"}
                    danach WebSocket verbinden, JSON-Events empfangen
  Abonnieren:       POST {userApplianceUri}/subscribe

Hinweis: Steuern (Kochen) läuft über Programm-Events + RemoteStart/Stop über den Socket und ist
hier NICHT implementiert – dieses Modul liest nur. Siehe README.

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

    def __init__(self, url, timeout=70, insecure=False):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        raw = socket.create_connection((host, port), timeout=timeout)
        if parts.scheme == "wss":
            # insecure: Gerät nutzt ein geräteübergreifendes, selbstsigniertes Zertifikat.
            context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
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


# --- Lokal (ohne Cloud, direkt im Heimnetz) --------------------------------
# Die Maschine bietet im LAN einen unauthentifizierten WebSocket auf 443 (TLS,
# selbstsigniert), der Events im Klartext streamt. Funktioniert auch ohne Kenwood-Cloud.


def find_local_ip(native_id):
    """Sucht die IP der Maschine in der ARP-Tabelle anhand der MAC (native_id ohne Trenner)."""
    import re
    import subprocess
    nid = native_id.lower().replace(":", "")
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = re.search(r"\(([0-9.]+)\) at ([0-9a-f:]+)", line)
        if not m:
            continue
        # macOS kürzt führende Nullen je Byte (d1:0), daher jedes Byte auf 2 Stellen bringen.
        mac = "".join(part.zfill(2) for part in m.group(2).split(":"))
        if mac == nid:
            return m.group(1)
    return None


def local_stream(ip, on_event, once=False, timeout=70):
    """Liest Events direkt von der Maschine im LAN (read-only, keine Cloud)."""
    ws = WebSocket(f"wss://{ip}:443/", timeout=timeout, insecure=True)
    try:
        while True:
            raw = ws.recv()
            if raw is None:
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
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


def _resolve_local_ip(args):
    if args.ip:
        return args.ip
    cfg = ks.load_env()
    _, _, native_id = user_appliance(cfg, ks.get_token(cfg), args.native_id)
    ip = find_local_ip(native_id)
    if not ip:
        sys.exit("Maschine nicht im LAN gefunden. IP per --ip angeben (arp -an).")
    return ip


def cmd_local_watch(args):
    ip = _resolve_local_ip(args)
    print(f"Lokal verbunden mit {ip} (ohne Cloud). Strg+C beendet.")
    local_stream(ip, lambda t, v, r: print(f"[{time.strftime('%H:%M:%S')}] {t} = {v}"), once=False, timeout=args.timeout)


def cmd_local_status(args):
    ip = _resolve_local_ip(args)
    state = {}
    deadline = time.time() + args.seconds
    def collect(etype, value, _raw):
        state[etype] = value
        if time.time() >= deadline:
            raise _Done()
    try:
        local_stream(ip, collect, once=False, timeout=args.seconds + 5)
    except _Done:
        pass
    print(json.dumps(state, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--native-id", help="MAC/native_id, falls mehrere Geräte am Konto hängen")
    parser.add_argument("--timeout", type=int, default=70, help="Sekunden Wartezeit auf WebSocket-Events")
    sub = parser.add_subparsers(required=True)

    p_status = sub.add_parser("status", help="Aktuellen Zustand als Schnappschuss ausgeben (Cloud)")
    p_status.add_argument("--seconds", type=int, default=8, help="Sammeldauer für den Schnappschuss")
    p_status.set_defaults(func=cmd_status)

    p_lw = sub.add_parser("local-watch", help="Live-Stream direkt aus dem LAN, ohne Cloud")
    p_lw.add_argument("--ip", help="IP der Maschine (sonst per ARP/Cloud ermittelt)")
    p_lw.set_defaults(func=cmd_local_watch)

    p_ls = sub.add_parser("local-status", help="Schnappschuss direkt aus dem LAN, ohne Cloud")
    p_ls.add_argument("--ip", help="IP der Maschine (sonst per ARP/Cloud ermittelt)")
    p_ls.add_argument("--seconds", type=int, default=8)
    p_ls.set_defaults(func=cmd_local_status)
    sub.add_parser("watch", help="Live-Zustand fortlaufend ausgeben").set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
