# Loxone Bridge – Home Assistant Custom Integration

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Dycon-Technologies-GmbH-Co-KG&repository=HomeAssistantLoxoneBridge&category=integration)

Bidirektionale Kommunikation zwischen **Home Assistant** und **Loxone Miniserver**.

Dieses Repository ist für die Veröffentlichung auf GitHub und für die Einbindung als benutzerdefiniertes **HACS-Repository** vorbereitet.

## Features

| Richtung | Beschreibung |
|---|---|
| **Loxone → HA** | Alle Loxone-Steuerungen werden automatisch als HA-Entitäten erkannt (Lichter, Schalter, Jalousien, Sensoren, Klima) |
| **HA → Loxone** | Alle HA-Entitäten werden automatisch an Loxone Virtuelle Eingänge gepusht |
| **Loxone → HA (Webhook)** | Loxone kann über Virtuelle HTTPS-Ausgänge HA-Geräte steuern |
| **🔒 Sicherheit** | HTTPS/WSS standardmäßig, Webhook-Kommandos nur von der konfigurierten Miniserver-IP |

### Unterstützte Loxone-Steuerungen

- **Licht**: Switch, Dimmer, ColorPickerV2, LightController, LightControllerV2
- **Schalter**: Switch, Pushbutton, TimedSwitch
- **Jalousien/Tore**: Jalousie (mit Lamellen), Gate, Window
- **Sensoren**: InfoOnlyAnalog, InfoOnlyDigital, TextState
- **Klima**: IRoomController, IRoomControllerV2
- **Alarm**: Alarm, SmokeAlarm, PresenceDetector

---

## Installation

### HACS (empfohlen)

1. Auf den HACS-Button oben klicken oder HACS öffnen → **Integrationen** → ⋮ → **Benutzerdefinierte Repositories**
2. Repository-URL `https://github.com/Dycon-Technologies-GmbH-Co-KG/HomeAssistantLoxoneBridge` eintragen und als **Integration** auswählen
3. **Loxone Bridge** installieren
4. Home Assistant neu starten

### Manuell

1. Den Ordner `custom_components/loxone_bridge/` in das HA-Verzeichnis `config/custom_components/` kopieren
2. Home Assistant neu starten

---

## Konfiguration

### 1. Integration hinzufügen

1. **Einstellungen** → **Geräte & Dienste** → **Integration hinzufügen**
2. Nach **Loxone Bridge** suchen
3. Verbindungsdaten eingeben:
   - **Host**: IP-Adresse des Miniservers (z.B. `192.168.1.100`)
   - **Port**: `443` (Standard für HTTPS)
   - **Benutzername**: Loxone-Benutzer
   - **Passwort**: Loxone-Passwort
   - **HTTPS/TLS verwenden**: ✅ (Standard, empfohlen)
   - **SSL-Zertifikat prüfen**: ❌ (Standard – Loxone nutzt selbstsignierte Zertifikate)
4. Sync-Optionen wählen:
   - ✅ **HA → Loxone**: Pusht HA-Entity-States an Loxone Virtuelle Eingänge
   - ✅ **Loxone → HA**: Registriert Webhook für Loxone Virtuelle Ausgänge

> **Hinweis Sicherheit:** Die Kommunikation läuft standardmäßig über HTTPS (Port 443) und WSS (WebSocket Secure). Benutzername und Passwort werden bei aktivem TLS verschlüsselt übertragen und nie in Logs oder Events im Klartext ausgegeben. Da Loxone Miniserver selbstsignierte Zertifikate verwenden, ist die Zertifikatsprüfung standardmäßig deaktiviert – die Verschlüsselung ist dennoch aktiv.

### 2. Loxone → Home Assistant (automatisch)

Nach der Einrichtung werden alle Loxone-Steuerungen automatisch als HA-Entitäten angelegt. Keine weitere Konfiguration nötig!

### 3. Home Assistant → Loxone (Virtuelle Eingänge)

Damit Loxone HA-Entity-Zustände empfangen kann:

1. **Loxone Config** öffnen
2. Für jede gewünschte HA-Entität einen **Virtuellen Eingang** erstellen
3. Benennung: `vi_<domain>_<entity_name>` (z.B. `vi_sensor_aussentemperatur`)
4. Die Integration pusht automatisch Zustandsänderungen an diese Eingänge

**Beispiel:** Für `sensor.outdoor_temperature` erstelle einen Virtuellen Eingang namens `vi_sensor_outdoor_temperature` in Loxone Config.

Für HA-Schalter (`switch.*`) einen digitalen Virtuellen Eingang bzw. Virtuellen Eingang Schalter verwenden. Die Integration sendet dafür `On`/`Off` und überträgt beim Start zusätzlich den aktuellen Zustand, damit Loxone nicht erst auf die nächste Zustandsänderung warten muss.

### 4. Loxone → Home Assistant steuern (Webhook)

Loxone kann HA-Geräte über HTTP-Befehle steuern:

Der Webhook akzeptiert Kommandos nur, wenn die Anfrage von der in der Integration konfigurierten Miniserver-Adresse kommt. Wenn Home Assistant hinter einem Reverse Proxy läuft, muss die Miniserver-Verbindung so eingerichtet sein, dass Home Assistant die echte Miniserver-IP als Quelladresse sieht.

1. Webhook-URL abrufen:
   ```yaml
   # In HA Developer Tools → Services:
   service: loxone_bridge.get_webhook_url
   ```

2. In **Loxone Config** einen **Virtuellen Ausgang** erstellen
3. Als URL den Webhook eintragen
4. Befehle als Query-Parameter oder JSON-Body senden

#### Einfache Befehle (Query-Parameter)

```
# Licht einschalten
https://<ha-ip>:8123/api/webhook/<webhook_id>?entity_id=light.wohnzimmer&state=on

# Licht ausschalten
https://<ha-ip>:8123/api/webhook/<webhook_id>?entity_id=light.wohnzimmer&state=off

# Dimmer auf 50%
https://<ha-ip>:8123/api/webhook/<webhook_id>?entity_id=light.wohnzimmer&state=50

# Thermostat auf 22°C
https://<ha-ip>:8123/api/webhook/<webhook_id>?entity_id=climate.heizung&state=22
```

#### Erweiterte Befehle (JSON Body)

```json
{
    "entity_id": "light.wohnzimmer",
    "action": "turn_on",
    "data": {
        "brightness": 200,
        "color_temp": 350
    }
}
```

#### Batch-Befehle

```json
{
    "commands": [
        {"entity_id": "light.wohnzimmer", "state": "on"},
        {"entity_id": "switch.ventilator", "state": "off"},
        {"entity_id": "cover.rolladen", "action": "set_cover_position", "data": {"position": 50}}
    ]
}
```

---

## Services

| Service | Beschreibung |
|---|---|
| `loxone_bridge.send_command` | Sendet einen Befehl an eine Loxone-Steuerung (UUID + Kommando) |
| `loxone_bridge.get_webhook_url` | Gibt die Webhook-URL für Loxone → HA aus |
| `loxone_bridge.generate_loxone_config` | Generiert Konfigurationsvorschläge für Virtuelle Ein-/Ausgänge |

### Beispiel: Loxone-Befehl senden

```yaml
service: loxone_bridge.send_command
data:
  uuid: "0f3c1a2b-0012-4fed-ffff-aabbccddeeff"
  command: "on"
```

---

## Architektur

```
┌─────────────────────┐     WSS (WebSocket Secure) ┌─────────────────────┐
│                     │ ◄──────────────────────── │                     │
│   Home Assistant    │     HTTPS Commands         │  Loxone Miniserver  │
│                     │ ────────────────────────► │                     │
│  ┌───────────────┐  │                            │  ┌───────────────┐  │
│  │ Loxone Bridge │  │    Virtual Inputs (HTTPS)  │  │ Virtual I/O   │  │
│  │  Integration  │──│───────────────────────────►│  │               │  │
│  │               │  │                            │  │               │  │
│  │               │◄─│────────────────────────────│──│ Virtual HTTPS │  │
│  │  (Webhook)    │  │    Virtual Outputs (HTTPS) │  │   Outputs     │  │
│  └───────────────┘  │                            │  └───────────────┘  │
└─────────────────────┘                            └─────────────────────┘
```

### Kommunikationswege

1. **WSS** (Loxone → HA): Verschlüsselter Echtzeit-Push von Loxone-Statusänderungen
2. **HTTPS Commands** (HA → Loxone): Verschlüsselte Steuerung von Loxone-Geräten
3. **Virtual Inputs** (HA → Loxone): Push von HA-Entity-Zuständen an Loxone via HTTPS
4. **Webhook** (Loxone → HA): Loxone steuert HA-Geräte via HTTPS

---

## Troubleshooting

### Verbindungsprobleme

- Sicherstellen, dass der Miniserver im Netzwerk erreichbar ist
- Port 443 (Standard für HTTPS) muss offen sein
- Benutzer muss ausreichende Rechte haben
- Falls SSL-Fehler auftritt: "SSL-Zertifikat prüfen" in den Optionen deaktivieren

### Virtuelle Eingänge funktionieren nicht

- Name muss exakt `vi_<domain>_<entity_name>` lauten
- Virtuelle Eingänge müssen in Loxone Config erstellt und gespeichert werden

### Webhook nicht erreichbar

- Home Assistant muss von Loxone aus erreichbar sein (gleiches Netzwerk)
- Webhook-URL prüfen via Service `loxone_bridge.get_webhook_url`

### Logs aktivieren

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.loxone_bridge: debug
```

---

## Lizenz

GNU Affero General Public License v3.0 (AGPL-3.0). Siehe [LICENSE](LICENSE).
