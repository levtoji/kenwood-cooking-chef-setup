// Gibt die SSIDs aller sichtbaren WLANs mit dem angegebenen Präfix aus (eine pro Zeile).
import CoreWLAN

let prefix = CommandLine.arguments.dropFirst().first ?? "KENWOOD"
guard let iface = CWWiFiClient.shared().interface() else { exit(1) }
let networks = (try? iface.scanForNetworks(withName: nil)) ?? []
for ssid in Set(networks.compactMap { $0.ssid }).sorted() where ssid.hasPrefix(prefix) {
    print(ssid)
}
