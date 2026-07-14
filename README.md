# Guesty Home Assistant Integration

Home Assistant Custom Component zur Anbindung der [Guesty Open API](https://open-api-docs.guesty.com/). Importiert alle Listings als Belegungs-Sensoren und Kalender für Automationen.

## Features

- **Automatischer Import aller Listings** – jedes Listing wird als Sensor und Kalender angelegt
- **Belegungs-Sensor pro Listing** – Status `vacant` (frei) oder `occupied` (vermietet)
- **Punktgenaue Check-in/out Updates** – zeitgesteuerte Neuberechnung ohne API-Polling
- **Guesty Webhooks** – gebündelte Echtzeit-Updates bei Reservierungs- und Listing-Änderungen
- **Signierte Webhooks** – eingehende Ereignisse werden mit Guestys Signatur
  geprüft; Duplikate und veraltete Zustellungen werden verworfen
- **Inkrementeller Sync** – nur geänderte Reservierungen + täglicher Vollabgleich
- **Traffic-sparsame Listing-Synchronisierung** – Listing-Payloads werden direkt verarbeitet; neue Listings laden nur ihre eigenen Reservierungen
- **Individuelle Check-in/Check-out Zeiten** – inkl. UTC-Fallback
- **Kalender pro Listing** – nutzbar in Automationen
- **Lokaler Cache mit Staleness-Erkennung** – transparent bei API-Ausfällen
- **Sync-Status-Sensor** – Diagnose der Integration
- **Custom Event** – `guesty_occupancy_changed` für Automationen
- **Diagnostics** – exportierbar über Home Assistant
- **API Retries** – exponentielles Backoff bei temporären Fehlern
- **Datenschutzmodus** – Gastnamen und Bestätigungscodes sind standardmäßig verborgen
- **Sicherer Gast-Türzugang** – ein zeitlich begrenzter Link pro Reservierung mit
  bis zu zwei serverseitig zugeordneten Home-Assistant-Schlössern und
  automatischer Browser-Sprache (Deutsch, Englisch, Spanisch oder Französisch)
- **Zugangslink-Diagnose pro Listing** – zeigt Link und Guesty-Syncstatus ohne
  den sensiblen Link in der Recorder-Historie zu speichern

## Voraussetzungen

- Home Assistant 2025.12 oder neuer
- Guesty Open API Zugang (Client ID + Client Secret)
- Für Webhooks: erreichbare externe Home Assistant URL (z. B. Nabu Casa)
- Für Gast-Türzugang: eine in Home Assistant eingetragene externe **HTTPS**-URL;
  HTTP wird aus Sicherheitsgründen abgelehnt

### API-Schlüssel erstellen

1. In Guesty einloggen
2. **Integrations** → **API & Webhooks**
3. Neue Application erstellen
4. **Client ID** und **Client Secret** sichern (Secret wird nur einmal angezeigt)

## Installation

### Über HACS (empfohlen)

1. HACS installieren (falls noch nicht vorhanden)
2. **HACS** → **Integrations** → **⋮** → **Custom repositories**
3. Repository-URL hinzufügen: `https://github.com/SVENS0Nb/Guesty-HA-Plugin`
4. Kategorie: **Integration**
5. **Guesty** suchen und installieren
6. Home Assistant neu starten

### Manuell

1. `custom_components/guesty` in dein Home Assistant `config/custom_components/` Verzeichnis kopieren
2. Home Assistant neu starten

## Einrichtung

1. **Einstellungen** → **Geräte & Dienste** → **Integration hinzufügen**
2. Nach **Guesty** suchen
3. **Client ID** und **Client Secret** eingeben
4. Optional: Aktualisierungsintervall anpassen (Standard: 300 Sekunden)

### Optionen

Über **Konfigurieren** auf der Integration:

| Option | Standard | Beschreibung |
|--------|----------|--------------|
| Reservierungs-Sync | 300 s | Wie oft Reservierungen abgeglichen werden |
| Listing-Sync | 86400 s | Sicherheitsabgleich bei aktiven Webhooks; ohne Webhook automatisch spätestens alle 15 Minuten |
| Vergangene Tage | 30 | Reservierungsfenster in die Vergangenheit |
| Zukünftige Tage | 365 | Reservierungsfenster in die Zukunft |
| Stale-Schwellenwert | 6 h | Ab wann Daten als veraltet gelten |
| Gastdetails anzeigen | Aus | Gastname und Bestätigungscode in Entitäten anzeigen; sensible Attribute werden nicht im Recorder gespeichert |
| Sicherer Gast-Türzugang | Aus | Erst nach weiterer Konfiguration werden Reservierungslinks erzeugt |
| Logo-URL | Leer | Optionales Logo oberhalb des Türportals; direkte HTTPS-Bild-URL |
| Favicon-URL | Leer | Optionales Browser-Icon des Türportals; direkte HTTPS-Bild-URL |

## Zeitlich begrenzter Gast-Türzugang

Die Integration kann pro Guesty-Listing ein oder zwei vorhandene
Home-Assistant-Entitäten aus der Domain `lock` zuordnen. Für jede aktive
Reservierung wird **ein** geschützter Link erzeugt. Auf der Seite erscheinen
Schaltflächen wie „Haustür öffnen“ oder „Wohnungstür öffnen“. Die Seite richtet
sich automatisch nach der bevorzugten Browser-/Systemsprache; Deutsch,
Englisch, Spanisch und Französisch werden unterstützt, alle anderen Sprachen
verwenden Englisch. Für jedes Schloss können in den Integrationsoptionen eigene
Bezeichnungen in allen vier Sprachen hinterlegt werden. Bekannte allgemeine
Namen wie „Haustür“ und „Wohnungstür“ erhalten lokale Übersetzungsvorschläge.
Unbekannte individuelle Namen bleiben als sicherer Fallback unverändert; es
werden keine Namen an einen externen Übersetzungsdienst übertragen.

Ein Aufruf des Links per `GET` öffnet niemals eine Tür. Erst eine kleine,
CSRF-geschützte `POST`-Anfrage nach einem bewussten Tastendruck kann
`lock.unlock` auslösen. Dabei werden Reservierungsstatus, Listing-Zuordnung,
Zeitfenster und Schlosszuordnung erneut serverseitig geprüft. Der Browser kann
keine beliebige Entity-ID übergeben. Eine erfolgreiche Aktion zeigt fünf
Sekunden lang eine kleine Bestätigung. Die Schloss-Schaltflächen bleiben
sichtbar und können anschließend ohne Neuladen erneut verwendet werden. Ein
abgelaufenes Aktions-Nonce wird erst beim nächsten Tastendruck erneuert und
einmal automatisch wiederholt; dadurch entsteht kein regelmäßiger
Hintergrund-Traffic.
Vor oder nach dem erlaubten Zeitraum erklärt die Seite in der erkannten Sprache,
dass sie nur im Buchungszeitraum verfügbar ist, ohne Reservierungsdetails
preiszugeben.

### Einrichtung

1. In Guesty unter **Operations → Portfolio → Custom fields → Reservations**
   ein Feld vom Typ **Text** anlegen, zum Beispiel `Door access link`.
2. Der Guesty-API-Anwendung Leserechte für Account-Custom-Fields sowie
   Schreibrechte für Reservierungs-Custom-Fields geben.
3. In Home Assistant bei der Guesty-Integration **Konfigurieren** öffnen und
   **Sicheren Gast-Türzugang** aktivieren.
4. Name oder ID des Custom Fields angeben. Der Standardname
   `Door access link` wird automatisch über die Guesty API aufgelöst.
5. Optional eine direkte HTTPS-URL für ein Logo und ein Favicon eintragen. Das
   Logo wird zentriert und responsiv mit maximal 96 px Höhe dargestellt.
6. Listings auswählen und jedem Listing ein oder zwei `lock.*`-Entitäten sowie
   gastfreundliche Türnamen auf Deutsch, Englisch, Spanisch und Französisch
   zuordnen. Die vorgeschlagenen Übersetzungen können frei angepasst werden.
7. Optional eine Freigabe vor Check-in oder nach Check-out einstellen.
8. In Guesty die erzeugte Custom-Field-Variable, zum Beispiel
   `{{door_access_link}}`, im Guest-App-Check-in-Text oder in einer
   automatisierten Nachricht verwenden.

Das Custom Field enthält ausschließlich die URL, damit Guesty sie als Link
darstellen kann. Die Integration verwendet dafür den aktuellen Guesty-v3-
Endpunkt für Reservierungs-Custom-Fields.

### Lebenszyklus und Ausfallsicherheit

- Bestätigte Reservierung: Token und Guesty-Link werden erzeugt.
- Datum, Listing oder Schlosszuordnung geändert: Der alte Token wird sofort
  ungültig und ein neuer Link wird veröffentlicht.
- Gelöschtes oder neu angelegtes Custom Field: Die gespeicherte Feld-ID wird
  nach einem Neustart neu geprüft. Bei einem Schreibfehler aktualisiert die
  Integration die ID, macht den alten Link ungültig und versucht genau einmal,
  einen neuen Link zu veröffentlichen.
- Stornierung, Löschung, Check-out oder deaktivierte Funktion: Der Zugriff wird
  zuerst lokal gesperrt; anschließend wird das Guesty-Feld gelöscht.
- Veraltete Guesty-Daten: Der Türzugang arbeitet „fail closed“ und verweigert
  die Öffnung, bis wieder aktuelle Reservierungsdaten vorliegen.
- Unveränderte Reservierungen erzeugen keine weiteren Guesty-Schreibzugriffe.
- Fehlgeschlagene Veröffentlichungs- und Löschvorgänge verwenden ein
  persistentes exponentielles Backoff. Dadurch erzeugen Guesty-Ausfälle keine
  Schreibschleife; lokal widerrufene Links bleiben dabei sofort gesperrt.
- Nicht mehr benötigte lokale Zugangsdatensätze werden nach erfolgreicher
  Bereinigung entfernt. Nicht bereinigbare Tombstones laufen nach sieben Tagen
  aus; ihre alten Links bleiben durch die lokale Token-Prüfung ungültig.
- „Synchronisiert“ wird erst gemeldet, nachdem der Wert über Guestys separaten
  Reservation-Custom-Field-GET-Endpunkt zurückgelesen wurde. Kurz verzögerte
  Guesty-Antworten werden begrenzt erneut geprüft.

### Reverse-Proxy-Sicherheit

- `/api/guesty/access/` muss `GET` und `POST` unverändert an Home Assistant
  weiterleiten und darf nicht gecacht werden.
- Für diesen Pfad keine zusätzliche Login-Seite des Reverse Proxys erzwingen;
  der lange Zufallstoken, das kurze Aktions-Nonce und die Zeitprüfung übernehmen
  die Gastautorisierung.
- Der Reservierungstoken steht in der URL. Access-Logs des Reverse Proxys für
  `/api/guesty/access/` deshalb deaktivieren oder den Pfad redigieren.
- TLS, korrekte `X-Forwarded-Proto`-/Host-Header und Home Assistants
  `trusted_proxies` korrekt konfigurieren.
- Logo und Favicon werden vom Browser direkt von den eingetragenen Hosts
  geladen. Für bestmöglichen Datenschutz die Bilder auf der eigenen
  HTTPS-Domain bereitstellen. Die Seite übermittelt dabei keinen Referrer.

Zusätzliche Schutzmaßnahmen sind ein 5-Sekunden-Cooldown, maximal zehn gültige
Aktionen pro Minute und Schloss, restriktive Browser-Header sowie das lokale
Event `guesty_door_access`. Das Event enthält Reservierungs-ID, Listing-ID,
Schloss-Entity und Ergebnis, aber weder Gastnamen noch Zugriffstoken.

## Entitäten

**Alle Listings** aus deinem Guesty-Konto werden automatisch importiert. Pro Listing:

| Entität | Beispiel | Beschreibung |
|---------|----------|--------------|
| Sensor | `sensor.ferienwohnung_belegung` | `vacant` oder `occupied` |
| Sensor (standardmäßig deaktiviert) | `sensor.ferienwohnung_aktueller_gast` | Name des Gastes der aktuell laufenden Reservierung |
| Diagnose-Sensor (standardmäßig deaktiviert) | `sensor.ferienwohnung_gast_zugangslink` | Status des aktuellen beziehungsweise nächsten Links; die erzeugte URL steht im Attribut `access_url` |
| Kalender | `calendar.ferienwohnung_reservierungen` | Alle Reservierungen |

Kalendereinträge zeigen standardmäßig nur „Reserviert“ und den Reservierungsstatus. Gastnamen und Bestätigungscodes können in den Integrationsoptionen aktiviert werden.

Der Sensor „Aktueller Gast“ benötigt ebenfalls die Option „Gastdetails anzeigen“ und ist zusätzlich standardmäßig deaktiviert. Nach dem manuellen Aktivieren wird sein Zustand vom Home-Assistant-Recorder gespeichert, sofern die Entität nicht in der Recorder-Konfiguration ausgeschlossen wird.

Der Sensor „Gast-Zugangslink“ ist aus Sicherheitsgründen ebenfalls
standardmäßig deaktiviert. Zum Prüfen unter **Einstellungen → Geräte & Dienste →
Guesty → Gerät des Listings → Entitäten** auch deaktivierte Entitäten anzeigen
und den Sensor aktivieren. Sein Zustand ist `Nicht konfiguriert`, `Keine
Reservierung`, `Ausstehend`, `Synchronisiert` oder `Fehler`. Der aktuelle oder
nächste Link steht in den Entitätsattributen unter `access_url`, zum Beispiel in
**Entwicklerwerkzeuge → Zustände**. `access_url` und der optionale Gastname werden
nicht in der Recorder-Historie gespeichert. Der Link ist dennoch ein
Zugangsschlüssel und darf nicht öffentlich geteilt oder in Dashboards für andere
Benutzer angezeigt werden.

Zusätzlich ein Integrations-Sensor:

| Entität | Beschreibung |
|---------|--------------|
| `sensor.guesty_sync_status` | `ok`, `degraded` oder `error` |

### Nicht benötigte Entitäten deaktivieren

1. **Einstellungen** → **Geräte & Dienste** → **Entitäten**
2. Nach Listing-Namen filtern
3. Entität deaktivieren

## Automationen

### Über Custom Event (empfohlen für Echtzeit)

```yaml
trigger:
  - platform: event
    event_type: guesty_occupancy_changed
    event_data:
      to: occupied
action:
  - service: notify.mobile_app
    data:
      message: "{{ trigger.event.data.listing_name }} ist jetzt belegt"
```

### Bei Check-out aufräumen

```yaml
trigger:
  - platform: state
    entity_id: sensor.ferienwohnung_belegung
    from: occupied
    to: vacant
action:
  - service: vacuum.start
    target:
      entity_id: vacuum.roborock
```

### Vor Check-in heizen

```yaml
trigger:
  - platform: calendar
    entity_id: calendar.ferienwohnung_reservierungen
    event: start
    offset: "-02:00:00"
action:
  - service: climate.set_temperature
    target:
      entity_id: climate.wohnzimmer
    data:
      temperature: 21
```

## Sync-Architektur

```mermaid
flowchart TD
    A[Guesty API] -->|Polling 5min| B[Coordinator]
    A -->|Webhook Push| C[Webhook Handler]
    C --> B
    B --> D[Lokaler Cache]
    B --> E[Belegungs-Sensoren]
    B --> F[Kalender]
    G[Transition Scheduler] -->|Check-in/out Zeit| B
```

1. **Polling** – inkrementeller Reservierungsabgleich alle 5 Minuten; verpasste Listing-Events werden ohne aktiven Webhook spätestens nach 15 Minuten erkannt
2. **Webhooks** – Guestys aktuelle Reservierungsereignisse
   `reservation.created.v2` und `reservation.updated.v2` werden nach einer
   kurzen 0,75-s-Sammelphase verarbeitet. Die Integration prüft die von Guesty
   bereitgestellte HMAC-Signatur; Duplikate und Ereignis-Bursts erzeugen dadurch
   möglichst wenige API-Aufrufe. Bestehende Abonnements ohne Signatur-Secret
   werden einmalig sicher neu erstellt; schlägt auch danach der Secret-Abruf
   fehl, bleibt der Polling-Fallback aktiv, ohne eine Neuanlage-Schleife.
3. **Scheduler** – Belegung wechselt punktgenau bei Check-in/out
4. **Täglicher Vollsync** – verhindert Drift im Cache

## Belegungslogik

Check-in/out Zeiten in dieser Priorität:

1. UTC-Felder `checkIn` / `checkOut` (wenn vorhanden)
2. `plannedArrival` / `plannedDeparture`
3. Listing-Defaults
4. Fallback: 15:00 / 11:00

## Tests

```bash
python -m pip install -r requirements-test.txt
python -m pytest
```

## Fehlerbehebung

- **Webhook nicht aktiv** – externe URL in Home Assistant konfigurieren (Einstellungen → System → Netzwerk)
- **Sync-Status `degraded`** – API temporär nicht erreichbar, Cache wird genutzt
- **Guesty-Feld bleibt leer** – den deaktivierten Diagnose-Sensor
  „Gast-Zugangslink“ aktivieren: `Synchronisiert` mit `access_url` bestätigt die
  lokale Erzeugung und den von Guesty bestätigten Schreibvorgang; `Ausstehend`
  oder `Fehler` zeigt, dass die Veröffentlichung noch nicht bestätigt ist
- **Diagnostics** – Integration → ⋮ → Diagnose-Daten herunterladen
- **Logs** – `logger: custom_components.guesty: debug` in `configuration.yaml`

## Lizenz

MIT
