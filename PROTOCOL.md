# Reverse-Engineering: Kenwood Cooking Chef XL (KCL95) / Drop · KitchenOS

Alles hier wurde durch Analyse der eigenen App-Installation und Beobachtung des **eigenen** Geräts
ermittelt (lawful interoperability / Neugier). Nichts davon stammt von Kenwood.
Backend ist die Drop-Plattform (`api.getdrop.io`, `*.getdrop.io`, `*.kitchenos.com`), OAuth-Client
„Kenwood World".

## 1. Einrichtung (SoftAP-Modus, nur während „WiFi → Verbinden")

Die Maschine spannt ein WLAN `KENWOOD<hex>` auf und ist unter `https://192.168.4.1` erreichbar
(Mongoose-Server, **HTTPS**, selbstsigniert; HTTP → 404).

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/mac` | `{"mac":"A8…"}` – native_id des Geräts |
| GET | `/networks` | Scan: `[{"ssid","rssi","security"}]` |
| POST | `/join` | Body `{"ssid","password"}` → Gerät verbindet sich |

Danach hängt das Gerät am Heim-WLAN und meldet sich bei der Cloud; die App-Verknüpfung passiert
über `GET {applianceUri}/connected?native_id=<mac>` (pollt bis `connection_state = CONNECTED`).

## 2. Cloud-Authentifizierung

OAuth 2.0 „password grant", `client_id`/`client_secret` fest in der App (BuildConfig):

```
POST {api}/api/v1/organizations/2/auth/token
  username, password, grant_type=password, client_id, client_secret
→ {access_token, refresh_token, user:{id}}
```

## 3. Telemetrie lesen (Cloud)

```
GET  {userUri}/events?client_id=…&type=ws   → {"events_url":"wss://events-shardsN.getdrop.io/ws/user/<JWT>"}
POST {userApplianceUri}/subscribe            (Gerät in den Event-Stream aufnehmen)
```
Der JWT steckt im WS-Pfad (Typ `events_channel`, enthält user_id). Event-Format:
```json
{"user_id":…,"appliance_id":"…","native_id":"A8…","step_id":null,"preset_id":null,
 "event":{"type":"Temperature","value":27,"timestamp":169…}}
```
Beobachtete/aus dem App-Enum bekannte `type`-Werte (Cloud, normalisiert):
`Temperature`, `TemperatureSetpoint`, `Heating`, `RemainingTime`, `Duration`, `ElapsedTime`,
`TimeProgressing`, `Program`, `ProgramProgress`, `ProgramFinished`, `Status` (z. B. `ready`),
`Setting`, `Weight`, `Power`, `Mode`, `RemoteControlEnabled`, `ReleasingPressure`,
`ActionRequiredMessage`, `AppliancePaired`.

## 4. Telemetrie lesen (lokal, OHNE Cloud)

Die Maschine betreibt im LAN auf **Port 443** einen WebSocket (TLS 1.2, selbstsigniertes,
geräteübergreifendes „Drop IoT"-Zertifikat, CN=Tim Redfern, 2018–2118). Er nimmt den Handshake
**ohne Authentifizierung auf jedem Pfad** an und streamt im Klartext. Lokales Format (eigenes
Vokabular, camelCase, mit mehreren Zeitstempeln):
```json
{"event":{"timestamp":2943971,"uitimestamp":2855358,"mgtimestamp":2971916,
          "type":"weightUpdate","value":180.0,"unit":"gram"}}
```
Im Leerlauf nur `weightUpdate`; weitere Typen bei Zustandsänderung. (→ `kenwood_control.py local-watch`)

## 5. Steuern (Befund: über die API nicht möglich)

- `POST {userApplianceUri}/features [{key,value}]` – akzeptiert serverseitig **nur** `RemoteWeighing`
  (Waage). Andere Felder → `422 / code 641 "Invalid feature"`. (Im App-Code nur vom kScale genutzt.)
- `POST {userUri}/appliances/{id}/operate {command,value}` – Befehle nur aus
  `{Power, Mode, Fan, Ion, Brightness, ChildLock, Filter}` (Luftreiniger etc.). Nicht-Geräteschalter → `500 KeyError`.
- Kochen läuft app-intern über **Programm-Events** + `RemoteStart`/`RemoteStop`/`RemotePause`
  (`StandMixerAppliance`), die bei `socket_type:"ws"` **nur empfangen** werden – der lokale/Cloud-WS
  der App ist Receive-only. Ein Remote-Start des Heizers/Motors ist für dieses Modell nicht
  vorgesehen (deckt sich mit Kenwoods „connected cooking nicht unterstützt").

Empirisch am Gerät bestätigt: Soll-Temperatur 50 via `features`/`operate` → abgelehnt, Maschine
blieb `ready`.

## 6. Firmware

Das Gerät meldet zwei Versionen: `iot_firmware_version` (WLAN-Modul) und `mcu_firmware_version`
(Steuerungs-Chip). OTA lädt das **Gerät selbst** über `GET {api}/api/v1/firmware?version=…`
(Endpunkt existiert, verlangt `version`); die Apps bauen diese Anfrage nicht. Ein Firmware-Image
haben wir nicht gezogen (kein Fuzzing des Hersteller-Endpunkts).

## 7. Sicherheit (Kurzfassung)

- Unauthentifizierter lokaler WebSocket, Klartext-Telemetrie an alle im WLAN.
- Kein gerätespezifisches TLS-Zertifikat → keine echte Authentifizierung lokal.
- Offen (nicht getestet): ob der lokale Kanal Befehle annimmt.
- Cloud: Fernzugriff hängt an Drop/KitchenOS; `client_secret` liegt in der App.

## 8. Was ohne Kenwood-Cloud bleibt

Kochen (eigenständiges Gerät) und lokales Auslesen (Abschnitt 4) funktionieren weiter; es entfallen
nur App-Fernzugriff, Rezept-Push und Kontobindung.
