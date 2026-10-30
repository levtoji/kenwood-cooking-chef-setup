# Kenwood Cooking Chef XL – WLAN-Einrichtung vom Mac

Die Cooking Chef XL (KCL95) lässt sich nur mit der alten **Kenwood World**-App ins WLAN bringen –
**Kenwood & Me** unterstützt die vernetzten Funktionen dieses Modells nicht. Dieses Skript macht
dasselbe wie die App, aber vom Mac aus.

## Was die App (und dieses Skript) tut

| Schritt | Ziel | Details |
|---|---|---|
| Login | `POST {api}/api/v1/organizations/2/auth/token` | OAuth `grant_type=password`, `client_id`/`client_secret` der App |
| Setup-WLAN | `KENWOOD…` (SoftAP der Maschine) | Maschine: Einstellungen → WiFi → Verbinden |
| MAC lesen | `GET https://192.168.4.1/mac` | **HTTPS**, selbstsigniertes Zertifikat; HTTP liefert nur 404 (Mongoose) |
| WLANs lesen | `GET https://192.168.4.1/networks` | `ssid`, `rssi`, `security` |
| WLAN senden | `POST https://192.168.4.1/join` | JSON `{"ssid": "...", "password": "..."}` |
| Verknüpfen | `GET {api}/api/v1/appliances/209/connected?native_id=<MAC>` | mit Bearer-Token pollen, bis `connection_state` = connected |

`{api}` ist standardmäßig `https://api.getdrop.io`.

## Voraussetzungen

- macOS, Python 3 (nur Standardbibliothek)
- Heim-WLAN mit **2,4 GHz** und **WPA2** (kein WPA3/Mischmodus); kurzes Passwort ist robuster
- `kenwood.env` mit `KW_CLIENT_ID` und `KW_CLIENT_SECRET` (siehe `kenwood.env.example`).
  Die Werte stehen in der Kenwood-World-APK (`com.adaptics.dropkitchen.base.BuildConfig`) und
  werden bewusst **nicht** in diesem Repo veröffentlicht.

## Benutzung

```sh
cp kenwood.env.example kenwood.env   # ausfüllen, inkl. KW_HOME_SSID
python3 kenwood_setup.py login        # einmalig in Terminal.app, Token landet in ~/.config/kenwood-setup/token.json
```

Danach: An der Maschine **Einstellungen → WiFi → Verbinden**, dann im Finder
**`Kenwood einrichten.command`** doppelklicken. Fertig.

Das Skript sucht das `KENWOOD…`-Setup-WLAN selbst (CoreWLAN via `wifi_scan.swift`, benötigt die
Xcode Command Line Tools), wechselt den Mac hinein und danach zurück. Manuell geht es auch:

```sh
python3 kenwood_setup.py setup [--ssid MEIN_24GHZ_WLAN] [--ap KENWOOD1A2B3C4]
```

Die Backend-Antwort bei Erfolg ist `{"connection_state": "CONNECTED"}`. Das WLAN-Passwort kommt aus dem
macOS-Schlüsselbund (macOS fragt einmal nach Erlaubnis) oder wird abgefragt.

**PIN auf dem Display:** Nach der Verbindung zeigt die Maschine eine PIN. Für die XL wird sie nicht
gebraucht – die Kontoverknüpfung passiert bereits über `/connected`. Der PIN-Weg der App
(`POST /api/v1/appliances/209/jit-pair`) wird vom Backend für dieses Modell mit
`400 "Appliance can't be paired this way"` abgelehnt; Kenwood & Me koppelt nur die KCL96 per Bluetooth.

Nur auslesen, ohne etwas zu senden (Mac vorher manuell ins Setup-WLAN):

```sh
python3 kenwood_setup.py probe
```

## Auslesen und steuern (`kenwood_control.py`)

Im Normalbetrieb läuft alles über die Cloud (lokal ist an der Maschine nur Port 443 offen, der
aber nicht per HTTP antwortet; die lokale `/mac`/`/networks`/`/join`-API gibt es nur im
Einrichtungsmodus unter `192.168.4.1`).

```sh
python3 kenwood_control.py status          # Schnappschuss, z. B. Temperatur/Status/Soll-Temp
python3 kenwood_control.py watch           # Live-Stream der Events (Strg+C beendet)
python3 kenwood_control.py set --temperature 80 --speed 2 --time 600
python3 kenwood_control.py operate --command <cmd> --value <val>
```

Protokoll:

| Zweck | Aufruf |
|---|---|
| Live-Events (WebSocket-URL) | `GET {userUri}/events?client_id=…&type=ws` → `{"events_url": "wss://…"}` |
| Abonnieren | `POST {userApplianceUri}/subscribe` |
| Einstellen | `POST {userApplianceUri}/features` mit `[{"key":"temperature","value":"80"}, …]` |
| Bedienen | `POST /api/v1/users/{id}/appliances/{applianceId}/operate` mit `{"command","value"}` |

Event-Format der Maschine:
`{"native_id":"…","event":{"type":"Temperature","value":27,"timestamp":…}}`.
Beobachtete `type`-Werte: `Temperature`, `TemperatureSetpoint`, `Status` (z. B. `ready`),
`RemainingTime`, `Setting`. Der WebSocket-Client ist minimal und kommt ohne Zusatzpakete aus.

> Hinweis: `set`/`operate` wurden bisher nur gegen einen simulierten Server getestet, nicht an der
> echten Maschine. Vor dem ersten echten Versuch nichts einspannen und danebenstehen.

## Fehlerbilder

- **E:34** auf dem Display: falsches WLAN-Passwort
- Maschine sieht das WLAN nicht: 5-GHz-only oder WPA3
- Setup-WLAN verschwindet: Einrichtungsmodus an der Maschine neu starten

## Alternative: Kenwood World auf neuem Android

Im Play Store ist die App für neue Android-Versionen nicht mehr installierbar. Die XAPK (z. B. von
APKPure) entpacken und per adb installieren:

```sh
unzip Kenwood+World_*.xapk -d kw
adb install-multiple kw/com.kenwood.world.apk kw/config.arm64_v8a.apk kw/config.en.apk kw/config.xhdpi.apk
```

## Hinweis

Inoffiziell, nicht von Kenwood/De'Longhi. Ermittelt durch Analyse der eigenen App-Installation zur
Einrichtung des eigenen Geräts. Nutzung auf eigenes Risiko.
