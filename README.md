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

## Auslesen (`kenwood_control.py`)

Im Normalbetrieb läuft alles über die Cloud (lokal ist an der Maschine nur Port 443 offen, der
nicht per HTTP antwortet; die lokale `/mac`/`/networks`/`/join`-API gibt es nur im
Einrichtungsmodus unter `192.168.4.1`).

```sh
python3 kenwood_control.py status            # Schnappschuss über die Cloud
python3 kenwood_control.py watch             # Live-Stream über die Cloud (Strg+C)
python3 kenwood_control.py local-status --ip <IP>   # Schnappschuss direkt im LAN, OHNE Cloud
python3 kenwood_control.py local-watch  --ip <IP>   # Live-Stream direkt im LAN, OHNE Cloud
```

Die `local-*`-Befehle reden direkt mit der Maschine im Heimnetz und brauchen Kenwoods Server nicht.
Ohne `--ip` wird die IP per ARP (und einmalig über die Cloud zur MAC-Ermittlung) gesucht.

Protokoll (lesen):

| Zweck | Aufruf |
|---|---|
| Live-Events (WebSocket-URL) | `GET {userUri}/events?client_id=…&type=ws` → `{"events_url": "wss://…"}` |
| Abonnieren | `POST {userApplianceUri}/subscribe` |

Event-Format: `{"native_id":"…","event":{"type":"Temperature","value":27,"timestamp":…}}`.
An der echten Maschine beobachtete `type`-Werte:

- `Temperature` – gemessene Ist-Temperatur (°C)
- `TemperatureSetpoint` – Soll-Temperatur (°C), 20–140 beobachtet
- `RemainingTime` – Timer in Sekunden (zählt 1/s herunter)
- `Status` – z. B. `ready`
- `Setting` – Rohwert (~675–686, vermutlich Waage/Encoder), noch nicht sicher gedeutet

Der WebSocket-Client ist minimal und kommt ohne Zusatzpakete aus.

## Steuern (Kochen) – noch nicht implementiert

Steuern ist komplexer als Auslesen und hier **bewusst nicht** umgesetzt:

- `.../features` (key/value) steuert nur die **Waage** (kScale), nicht die Cooking Chef.
- `.../operate` kennt nur Geräte-Schalter (`Power, Mode, Fan, Ion, Brightness, ChildLock, Filter`),
  nicht das Kochen.
- Die Cooking Chef (`StandMixerAppliance`) wird über **Programm-Events** (Temperatur, Rührstufe,
  Zeit) plus die Kommandos **`RemoteStart` / `RemoteStop` / `RemotePause`** gesteuert, die über den
  Socket gepublished werden (bidirektional, nicht per einfachem REST-POST).

Das sauber umzusetzen heißt, den Heizer und den Motor fernauszulösen – daher erst nach einem
Protokoll-Nachbau und nur mit leerer Schüssel und Aufsicht testen.

**Empirisch bestätigt (am echten Gerät, leere Schüssel):** Fernsteuern des Kochens ist über diese
Cloud-API nicht möglich. `POST {userApplianceUri}/features` akzeptiert laut App und Server nur das
Feld `RemoteWeighing` (Waage); jedes andere Feld wird mit `422 / code 641 "Invalid feature"`
abgelehnt, `operate` mit einem Nicht-Geräteschalter mit `500 KeyError`. Die Maschine blieb dabei
unverändert auf `ready`. Die App schiebt Rezepte nur aufs Display – gestartet wird am Gerät.

## Sicherheit und Langlebigkeit (Befunde am eigenen Gerät)

**Lokaler WebSocket ohne Authentifizierung.** Die Maschine öffnet im Heimnetz auf Port 443 (TLS 1.2)
einen WebSocket, der den Handshake auf *jedem* Pfad ohne Login annimmt und laufend Events im
Klartext streamt (z. B. `{"event":{"type":"weightUpdate","value":178.0,"unit":"gram"}}`). Jeder im
selben WLAN kann also mitlesen. Das TLS-Zertifikat ist nicht gerätespezifisch, sondern ein
geräteübergreifendes, selbstsigniertes „Drop IoT"-Zertifikat (gültig 2018–2118) und bietet daher
keine echte Authentifizierung.

*Offen (bewusst nicht getestet):* ob dieser lokale Kanal auch **Befehle annimmt** – dann könnte
jemand im WLAN die Maschine steuern. Das lässt sich nur mit Aufsicht am Gerät prüfen, da es Motor
oder Heizung auslösen könnte.

Praktische Konsequenz: Gäste-WLAN für IoT nutzen bzw. die Maschine vom Hauptnetz trennen, wenn das
Mitlesen stört.

**Was passiert, wenn Kenwood die Server abschaltet?**

- **Kochen bleibt unberührt.** Die Cooking Chef ist ein eigenständiger Standmixer mit eigener
  Bedienung; die Cloud liefert nur App-Komfort (Monitoring, Rezepte aufs Display).
- **Lokales Auslesen bleibt möglich.** Der LAN-WebSocket funktioniert ohne Cloud – siehe
  `local-watch`/`local-status`. Ein selbstgehostetes Tool kann die Maschine also weiter überwachen.
- **Nur die Cloud-Features sterben:** App-Fernzugriff von unterwegs, Rezept-Push, Konto-Bindung.

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
