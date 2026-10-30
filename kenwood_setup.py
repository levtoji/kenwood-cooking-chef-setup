#!/usr/bin/env python3
"""Richtet eine Kenwood Cooking Chef XL (KCL95) vom Mac aus ins WLAN ein.

Ablauf wie in der Kenwood-World-App (SoftAP-Onboarding):
  1. Login am Kenwood/Drop-Backend (OAuth password grant)
  2. Mac verbindet sich mit dem Setup-WLAN der Maschine (KENWOOD…)
  3. https://192.168.4.1/mac, /networks lesen, POST /join {ssid, password}
  4. Mac zurück ins Heim-WLAN
  5. Backend pollen, bis die Maschine verbunden ist und am Konto hängt

Nur Standardbibliothek. App-Schlüssel kommen aus kenwood.env (nicht im Repo).
"""

import argparse
import getpass
import json
import os
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APPLIANCE_HOST = "https://192.168.4.1"
COOKING_CHEF_APPLIANCE_URI = "/api/v1/appliances/209"
ORGANIZATION_ID = "2"
WIFI_INTERFACE = "en0"
TOKEN_FILE = Path.home() / ".config" / "kenwood-setup" / "token.json"
ENV_FILE = Path(__file__).resolve().parent / "kenwood.env"


def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    missing = [k for k in ("KW_CLIENT_ID", "KW_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Fehlt in {ENV_FILE.name}: {', '.join(missing)} (siehe kenwood.env.example)")
    return {
        "api": os.environ.get("KW_API_BASE_URL", "https://api.getdrop.io").rstrip("/"),
        "client_id": os.environ["KW_CLIENT_ID"],
        "client_secret": os.environ["KW_CLIENT_SECRET"],
    }


def http(method, url, data=None, headers=None, form=False, insecure=False, timeout=15):
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    headers.setdefault("User-Agent", "Kenwood World")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    # Die Maschine nutzt ein selbstsigniertes Zertifikat, die App prüft es ebenfalls nicht.
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            text = response.read().decode() or "{}"
            return response.status, json.loads(text) if text.strip().startswith(("{", "[")) else text
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


# --- Backend ---------------------------------------------------------------


def token_url(cfg, action="token"):
    return f"{cfg['api']}/api/v1/organizations/{ORGANIZATION_ID}/auth/{action}"


def save_token(token):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token))
    TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def login(cfg, email=None):
    if not sys.stdin.isatty():
        sys.exit("Login braucht ein interaktives Terminal (Passwort-Abfrage). Bitte in Terminal.app ausführen.")
    email = email or input("Kenwood-Konto E-Mail: ").strip()
    password = getpass.getpass("Kenwood-Konto Passwort: ")
    status, body = http(
        "POST",
        token_url(cfg),
        {
            "username": email,
            "password": password,
            "grant_type": "password",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        form=True,
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        sys.exit(f"Login fehlgeschlagen ({status}): {body}")
    save_token(body)
    print(f"Login ok (User-ID {body.get('user', {}).get('id', '?')}).")
    return body


def refresh(cfg, token):
    status, body = http(
        "POST",
        token_url(cfg),
        {"refresh_token": token["refresh_token"], "grant_type": "refresh_token", "client_id": cfg["client_id"]},
        form=True,
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        return None
    token = {**token, **body}
    save_token(token)
    return token


def get_token(cfg):
    if TOKEN_FILE.exists():
        token = json.loads(TOKEN_FILE.read_text())
        if token.get("refresh_token"):
            refreshed = refresh(cfg, token)
            if refreshed:
                return refreshed
    return login(cfg)


def connected_url(cfg, mac):
    query = urllib.parse.urlencode({"native_id": mac})
    return f"{cfg['api']}{COOKING_CHEF_APPLIANCE_URI}/connected?{query}"


def auth_header(token):
    return {"Authorization": f"Bearer {token['access_token']}"}


def wait_for_cloud(cfg, token, mac, timeout_s):
    url = connected_url(cfg, mac)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(10)
        status, body = http("GET", url, headers=auth_header(token))
        state = body.get("connection_state") if isinstance(body, dict) else body
        print(f"  Backend: {status} {state}")
        if status == 401:
            token = refresh(cfg, token) or login(cfg)
            continue
        if status == 200 and str(state).lower() == "connected":
            return True
    return False


# --- WLAN am Mac -----------------------------------------------------------


def current_ssid():
    out = subprocess.run(["ipconfig", "getsummary", WIFI_INTERFACE], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.strip().startswith("SSID :"):
            return line.split(":", 1)[1].strip()
    return None


def keychain_password(ssid):
    result = subprocess.run(["security", "find-generic-password", "-wa", ssid], capture_output=True, text=True)
    return result.stdout.strip() or None


def find_setup_ap(prefix="KENWOOD", timeout_s=60):
    """Sucht das Setup-WLAN der Maschine per CoreWLAN (networksetup kann nicht scannen)."""
    scanner = Path(__file__).resolve().parent / "wifi_scan.swift"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = subprocess.run(["swift", str(scanner), prefix], capture_output=True, text=True)
        found = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if found:
            if len(found) > 1:
                print(f"  Mehrere Setup-WLANs gefunden: {', '.join(found)} – nehme {found[0]}")
            return found[0]
        time.sleep(3)
    return None


def join_wifi(ssid, password=None):
    cmd = ["networksetup", "-setairportnetwork", WIFI_INTERFACE, ssid]
    if password:
        cmd.append(password)
    for _ in range(3):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if not result.stdout.strip() and result.returncode == 0:
            time.sleep(5)
            return True
        time.sleep(3)
    print(f"  networksetup: {result.stdout.strip() or result.stderr.strip()}")
    return False


def wait_for_appliance(timeout_s=60):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, body = http("GET", f"{APPLIANCE_HOST}/mac", insecure=True, timeout=5)
            if status == 200 and isinstance(body, dict) and body.get("mac"):
                return body["mac"]
        except OSError:
            pass
        time.sleep(2)
    return None


# --- Befehle ---------------------------------------------------------------


def cmd_login(args):
    login(load_env(), args.email)


def cmd_setup(args):
    cfg = load_env()
    token = get_token(cfg)

    home_ssid = args.ssid or os.environ.get("KW_HOME_SSID") or input("Heim-WLAN für die Maschine (2,4 GHz): ").strip()
    home_password = keychain_password(home_ssid) or getpass.getpass(f"WLAN-Passwort für {home_ssid}: ")
    # Passwort für die Rückkehr ins Heim-WLAN: dasselbe Netz, sofern nichts anderes angegeben.
    return_ssid = args.return_ssid or home_ssid
    return_password = home_password if return_ssid == home_ssid else keychain_password(return_ssid)

    print("1/4 Verbinde Mac mit Setup-WLAN der Maschine …")
    if wait_for_appliance(timeout_s=3):
        print("  Mac ist bereits im Setup-WLAN.")
    else:
        ap = args.ap or find_setup_ap()
        if not ap:
            sys.exit("Kein KENWOOD…-WLAN gefunden. An der Maschine Einstellungen → WiFi → Verbinden starten.")
        print(f"  Setup-WLAN: {ap}")
        if not join_wifi(ap, args.ap_password):
            sys.exit(f"Konnte nicht mit {ap} verbinden. Mac im WLAN-Menü manuell verbinden und erneut starten.")

    try:
        print("2/4 Lese Maschine aus …")
        mac = wait_for_appliance()
        if not mac:
            sys.exit(f"Maschine antwortet nicht auf {APPLIANCE_HOST}/mac.")
        print(f"  MAC: {mac}")

        status, body = http("GET", f"{APPLIANCE_HOST}/networks", insecure=True)
        networks = body.get("networks", []) if isinstance(body, dict) else []
        visible = {n.get("ssid"): n for n in networks}
        print(f"  Sichtbare WLANs: {', '.join(sorted(s for s in visible if s))}")
        if home_ssid not in visible:
            sys.exit(f"Die Maschine sieht {home_ssid} nicht (nur 2,4 GHz / WPA2 wird unterstützt).")

        print(f"3/4 Sende WLAN-Daten für {home_ssid} …")
        status, body = http("POST", f"{APPLIANCE_HOST}/join", {"ssid": home_ssid, "password": home_password}, insecure=True)
        print(f"  join: {status} {body}")
        if status != 200:
            sys.exit("Die Maschine hat die WLAN-Daten abgelehnt.")
    finally:
        print(f"4/4 Mac zurück ins WLAN {return_ssid} …")
        join_wifi(return_ssid, return_password)

    print("Warte auf Verbindung der Maschine mit dem Backend …")
    if not wait_for_cloud(cfg, token, mac, args.timeout):
        sys.exit("Zeitüberschreitung: Maschine meldet sich nicht im Backend. Display der Maschine prüfen (z. B. E:34 = falsches Passwort).")
    print("Fertig: Maschine ist verbunden und mit deinem Konto verknüpft.")


def cmd_probe(args):
    """Nur lesen: zeigt MAC und sichtbare WLANs, während der Mac im Setup-WLAN ist."""
    for path in ("/mac", "/networks"):
        status, body = http("GET", f"{APPLIANCE_HOST}{path}", insecure=True, timeout=5)
        print(f"GET {path}: {status} {json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else body}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)

    p_login = sub.add_parser("login", help="Am Kenwood-Konto anmelden und Token lokal speichern")
    p_login.add_argument("--email")
    p_login.set_defaults(func=cmd_login)

    p_setup = sub.add_parser("setup", help="Maschine ins WLAN bringen und mit dem Konto verknüpfen")
    p_setup.add_argument("--ssid", help="Heim-WLAN für die Maschine (2,4 GHz, WPA2); Standard: KW_HOME_SSID")
    p_setup.add_argument("--ap", help="Setup-WLAN der Maschine; Standard: automatisch KENWOOD… suchen")
    p_setup.add_argument("--ap-password", help="Passwort des Setup-WLANs, falls nicht offen")
    p_setup.add_argument("--return-ssid", help="WLAN, in das der Mac danach zurückkehrt (Standard: --ssid)")
    p_setup.add_argument("--timeout", type=int, default=300, help="Sekunden Warten auf das Backend")
    p_setup.set_defaults(func=cmd_setup)

    p_probe = sub.add_parser("probe", help="Maschine nur auslesen (Mac muss im Setup-WLAN sein)")
    p_probe.set_defaults(func=cmd_probe)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
