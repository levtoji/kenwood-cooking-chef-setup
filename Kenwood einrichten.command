#!/bin/zsh
# Doppelklick im Finder: Cooking Chef XL ins WLAN bringen.
# Vorher an der Maschine: Einstellungen → WiFi → Verbinden.
cd "${0:A:h}"
python3 kenwood_setup.py setup
echo
read -k1 "?Taste drücken zum Schließen …"
