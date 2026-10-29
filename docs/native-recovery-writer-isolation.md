# Ownership- und External-Writer-Isolation (PR #100)

## Geltungsbereich

Diese Änderung liefert ausschließlich interne, unveränderliche diagnostische
Ownership-/Writer-Evidenz. `native_activation_allowed` und `coordination_ready`
bleiben `False`. Es gibt keinen positiven Cycle-Entscheid, kein Permit, keinen
Scheduler-Start, keine Ersatzbewässerung und keine neue Pumpenansteuerung.
Pumpen-, Shot-, Cycle-, Accounting- und Recovery-Sicherheitsgates bleiben erhalten.
Historische Operationen, Zähler, Optionen und Journale werden nicht verändert.

Grundlage sind `native-recovery-release-design.md`,
`native-recovery-participant-gates.md` und `native-recovery-revisions.md` sowie
die bestehenden Recovery-Transaction-/Release-Verträge. Deren technische
Receipts und Fake-Cycle-Entscheidungen sind keine Exklusivitätsnachweise.

## Inventar der Schreibpfade und kontrollierbare Grenzen

| Writer/Pfad | Bestehende Kontrolle | Grenze |
| --- | --- | --- |
| `PumpController._service()` | Serialisierte interne Aktivierung, Zeitgrenzen, fest gebundenes Ziel, Journal und OFF-Bestätigung; `switch.turn_on`/`switch.turn_off` | Ein lokaler Lock blockiert keine fremden Services oder Gerätebefehle; erfolgreiche Softwareantwort ist kein physischer Nachweis. |
| `_ShotCoordinator` | Owner-Prüfung auf Pumpen- und Shot-Seite, unveränderliche Reservierung, Generationen, Revalidierung, Journal/Accounting; verwendet den PumpController | Die bisherige `OwnershipEvidence` enthält explizite Harness-Prüfannahmen. Auch `exclusive_control_verified=True` ist keine neue produktive Writer-Attestation. |
| `_NativeScheduler` und Cycle | Eigene Registrierungen, Generationen, Timer, Admission-/Retry- und Cycle-Gates; Ansteuerung über ShotCoordinator | Kein globaler HA-Admission-Lock. Die Diagnose startet keinen Scheduler und ruft keine mutierenden `permits()` auf. |
| Legacy-Blueprint `shot_engine.yaml` | Eigene YAML-Bedingungen, `homeassistant.turn_on` und normale/verzögerte `homeassistant.turn_off` | Unabhängiger Writer; interne Python-Locks und Scheduler-Stop kontrollieren ihn nicht. Bereits geplante OFF-Aktionen können später eintreffen. Keine automatische Deaktivierung. |
| Integrationsservice `stop_pump` in `__init__.py` | `_stop_pump_for_entry()` ruft `_call_helper_service()` für das konfigurierte Switch-/Input-Boolean-Ziel auf | Umgeht den Entwicklungs-PumpController. Auch OFF ist ein konkurrierender Schreibzugriff. Andere Helferservices ändern Flags/Zähler/Referenzen, keine Pumpen-ON-Befehle. |
| HA-Automationen, Skripte, UI, REST/WebSocket und andere Servicecaller | HA-Serviceereignisse können einen erfolgreichen Dispatch-Versuch anzeigen | Dynamische Templates, verschachtelte Skripte, Gruppen, Areas und Geräteziele ergeben keinen vollständigen Writer-/Zielgraphen. Context-/User-/Parent-IDs sind Hinweise, keine beglaubigte Owner-Identität. |
| Andere Integrationen | Explizite Entity-Ziele in beobachteten Serviceereignissen sind erkennbar; Statusberichte können sichtbar werden | Direkte Entity-Methoden, Coordinator-/Geräteclients und integrationsspezifische indirekte Befehle müssen kein passendes Serviceereignis erzeugen. |
| Direkter Aktorzugriff, Geräte-/Netzwerkkommunikation, physische Bedienung | Allenfalls spätere HA-Statusberichte | MQTT-/Vendor-API-/Gateway-/Funk-/Hardwarezugriff und Befehle ohne Zustandsänderung liegen außerhalb der Integrationsgrenze. |
| Direkte Dateischreiber | Vorhandene Journal-Receipt-/Payload-Prüfung und erneute Reads erkennen bestimmte Änderungen | Dateibesitz und atomare Einzelwrites garantieren keine exklusive Schreibhoheit; kein Fencing privilegierter Prozesse. |

Die normale Integration konstruiert die Entwicklungskomponenten bzw. den
RecoveryGateCoordinator nicht. Nur dessen explizite interne Konstruktion
installiert die lokale Writer-Beobachtung. Es werden weder produktive Services
ersetzt noch globale Service-Registrierungen verändert. Keine fremde Automation
wird gestoppt, gesperrt oder repariert.

## Tatsächliche HA-Beobachtung

`_WriterObserver` verwendet ausschließlich entfernbare Event-Bus-Listener für
`call_service`, `state_changed` und `homeassistant_stop` während der Lebensdauer
des internen Recovery-Koordinators. Der bestehende Koordinator beobachtet
zusätzlich Zustands-/Konfigurationsänderungen. Es gibt keinen Monkeypatch,
keinen Service-Hook und keinen Befehlsfilter im Produktivcode.

Im getesteten HA 2025.12.5 wird `call_service` nach Serviceauflösung und
Schema-Prüfung, vor Ausführung des Handlers ausgelöst. Das Ereignis beweist
einen Versuch, weder erfolgreiche Ausführung noch Aktorzustand. Vorher
abgewiesene, nie dispatchte oder direkt am Gerät gesendete Aufrufe sind dadurch
nicht vollständig sichtbar. Async-Callbacks sind keine atomare Sperre gegenüber
Serviceausführung; ein noch nicht verarbeiteter Callback kann keine Exklusivität
begründen.

Explizite Pumpen-Entity-Ziele, Entity-Listen, kommaseparierte IDs und `all` werden
konservativ als Konflikt erfasst, unabhängig von Servicename oder Domain. Dazu
gehören auch OFF und scheinbar interne Contexts: Es existiert keine sichere
Context-Whitelist. `switch`-, `homeassistant`-, `script`- und `automation`-Aufrufe
mit ungeklärtem Ziel widerrufen ebenfalls und bleiben als unbekannter Einfluss
sichtbar. Andere indirekte Custom-Services sind nicht vollständig erfassbar.

Ein Pumpen-`state_changed` ohne nachgewiesenen exklusiven Writer ist ein
Konflikt; eine Statusmeldung allein identifiziert keinen tatsächlichen Writer.
Die Aufzeichnungen heißen deshalb untrusted observations, nicht authentifizierte
Fremdidentitäten. HA-Berichte können verspätet, zusammengefasst oder restauriert
sein. Unveränderte Berichte können nur `state_reported` statt `state_changed`
erzeugen. Letzteres wird nicht als vollständiger Überwachungspfad abonniert;
bereits vorhandene Snapshot-Prüfungen erfassen aktuelle Berichtstimestamps.
Auch ein solches zusätzliches Abonnement würde Direktzugriffe nicht ausschließen.

Eine bekannte Monitoring-Lücke, Listener-Installation mit Fehler, expliziter
Monitor-Stop oder HA-Stop erhöht die Beobachtungsgeneration und widerruft sofort
die bisherige Diagnose. Der Verlauf hält höchstens 128 Beobachtungen; Überlauf
setzt eine bleibende Lücke. Konflikte bleiben auch bei Überlauf sichtbar. Nicht
erkennbare Eventverluste und die Zeit vor Monitorbeginn werden niemals als
erfolgreiche Überwachung interpretiert: Sichtbarkeit und externe Writer bleiben
grundsätzlich unbewiesen. Fehlende Events sind kein Ausschlussnachweis.

## Datenmodell und Reason-Codes

`recovery_writer.py` definiert eingefrorene Dataclasses mit Tupeln:

* `WriterObservation`: Ereignistyp, Domain/Service, Context-/Parent-/User-Hinweise,
  Erfassungszeit und konservative Konfliktklassifikation.
* `WriterIsolationEvidence`: Config Entry/Pumpenressource (`ResourceBinding`),
  komplette `RuntimeEpochs` mit Boot-/Controller-/Ressourcenidentität,
  Ownership- und Safety-Generation, vollständige `EvidenceRevision`-Tupel der
  Recovery-Anfrage, Attempt-ID und eindeutige aktuelle interne Controller-ID.
* Zusätzlich Monitor-ID, monotone Beobachtungsgeneration, Monitorbeginn/-status,
  erfasste Writer/Konflikte, explizit unbekannte Writer-Klassen, Prüfzeit und
  Gültigkeitsende (30 Sekunden ab Anfrage) sowie typisierte `WriterReason`-Codes.

Die Writer-Beobachtung erzeugt keine unabhängigen Journalbelege. Sie bindet an
die vorhandenen dauerhaften Receipts und alle Teilnehmerrevisionen der Anfrage.
Pumpennachweis, Request, Receipt und Runtime werden weiterhin mit den aktuellen
Teilnehmern verglichen. Eine passive Prüfung schreibt keine neue Revision.

| Aussage/Code | Bedeutung |
| --- | --- |
| `internal_owner_known` | Aktueller lokaler PumpController ist registriert, initialisiert, nicht geschlossen und seine Owner-Evidenz passt zum Ziel. Die ID ist seine Controller-Epoche. Keine Aussage über Fremdausschluss. |
| `no_foreign_writers_observed` | Im begrenzten Beobachtungsfenster liegt keine verdächtige Writer-Beobachtung vor. Keine Aussage über unbeobachtete Pfade. |
| `writer_conflict_detected` | Ein untrusted pumpenbezogener Schreibversuch oder Statusbericht wurde beobachtet. Nachweis widerrufen; keine sichere Attribution an einen fremden Benutzer behauptet. |
| `writer_visibility_incomplete` | HA-Beobachtung deckt nicht alle Pfade ab; gilt auch bei aktivem, bisher stillem Monitor. |
| `ownership_epoch_changed` | Ziel, Boot-/Controller-/Ownership-/Safety-Epoche passen nicht mehr zur Anfrage oder wurden während der Sammlung verändert. |
| `external_writers_unproven` | Externer Writer-Ausschluss bleibt unbewiesen und blockiert ausnahmslos. |
| `exclusive_isolation_confirmed` | Für einen späteren wirksamen Fencing-Vertrag reserviert; kein aktueller Aussteller und kein aktueller positiver Zustand. |
| `internal_owner_unknown` | Kein passender aktueller interner Owner-Nachweis. |
| `writer_evidence_expired` | Prüfzeit ist zukünftig oder älter als 30 Sekunden. |
| `revision_conflict` | Dauerhafte Pumpenrevision/Receipt passt nicht zur Anfrage; bestehende vollständige Teilnehmerprüfung bleibt zusätzlich bestehen. |

`exclusive_isolation_confirmed` und `native_activation_allowed` sind
nicht initialisierbare, konstante `False`-Felder. Weder ein bekannter Owner noch
Stille, ein erfolgreicher FakePump-Test oder ein frei konstruierter Modellbeleg
stellt einen positiven Exklusivitätsnachweis aus.

## Recovery-Bindung, Widerruf und ABA

Die Pumpenteilnehmer-Evidenz trägt `writer_isolation`. Der Koordinator sammelt
diese Evidenz erneut beim finalen Vergleich und bei `confirmation_current()`;
die Monitoridentität/-generation/-aktivität ist Teil des Guards. Beobachtete
Konflikte und bekannte Lücken rufen synchron den diagnostischen Widerruf auf:
Generation erhöhen, aktuelle Anfrage und gecachte Bestätigung verwerfen.
Teilnehmer-Sicherheitsflags, Pumpenbefehle und Journale werden nicht angefasst.

Boot, Controller-Neuanlage, Ownership-Wechsel, Konfigurations-/Pumpenzielwechsel
und Safety-/Journaländerungen entwerten die alten Bindungen. Ownership A–B–A
erhöht Generationen trotz gleichem Endwert. Pumpenstatus A–B–A erhöht die
Event-/Monitorgeneration. Ein Neustart erzeugt neue Boot-/Controller-/Monitor-IDs;
keine Writer-Bestätigung wird persistiert oder wiederhergestellt. Bestehende
dauerhafte Revisionen verhindern zusätzlich Journal-ABA. Direkte unbemerkte
Manipulation privater HA-/Python-Strukturen liegt außerhalb dieses Vertrags.

Änderungen während eines Await verwerfen die ganze Sammlung (`evidence_changed`),
inklusive eigens ausgewiesener Epochwechsel. Fehlerpfade behalten die expliziten
Visibility-/External-Writer-Blocker. Fehlende oder widersprüchlich gebundene
Writer-Evidenz kann keine Koordinationsbereitschaft erzeugen. Aggregation bleibt
eine Vereinigung von Blockern. `ownership_epoch_unproven` bleibt bestehen: Die
jetzt gebundene lokale Epoche beweist noch keine exklusive Aktorhoheit.

`confirmation_current()` bedeutet weiterhin nur, dass genau diese diagnostische
Sammlung lokal aktuell ist. Auch eine aktuelle Diagnose kann ausdrücklich eine
Monitoring-Lücke oder einen Writer-Konflikt enthalten und ist niemals eine
Autorisierungsprüfung. Hätte ein Gerät ohne HA-Ereignis einen Befehl erhalten,
bleiben `writer_visibility_incomplete` und `external_writers_unproven` blockierend,
auch wenn der lokale Ereignisverlauf unverändert aussieht.

## Voraussetzungen für echte Isolation

Eine spätere positive Attestation benötigt einen exklusiven Aktor-/Gateway-Vertrag:

1. Alle physisch möglichen Befehlswege einschließlich Vendor-API, MQTT, manueller
   Zugriffe und Integration-Clients müssen am selben exklusiven Enforcer enden.
2. Authentifizierte Owner-Leases mit dauerhaften, monotonen Fencing-Tokens müssen
   für jeden Befehl geprüft werden. Alte und konkurrierende Tokens werden am
   Aktor abgewiesen, auch bei Neustart, Netzpartition, verspätetem Befehl und ABA.
3. Admission-Sperre, Ablaufzeit, Quiesce/Drain und bestätigter Widerruf müssen
   ausstehende/gespeicherte Gerätebefehle umfassen. Verbindungsverlust sperrt.
4. Der Enforcer muss eine frische, ressourcen-/boot-/owner-/revisionsgebundene,
   überprüfbare Attestation liefern. Lücken oder Lease-Verlust widerrufen diese.
5. Physisch unabhängiges OFF und die verbleibenden fachlichen Recovery-,
   Accounting-, Baseline- und Commit-Nachweise sind weiterhin separat erforderlich.

Eine weitere HA-Boolean-Option, Registry-Suche oder Context-Korrelation erfüllt
diesen Vertrag nicht. Der aktuelle PR implementiert keinen solchen Aussteller.

## Validierung

Baseline nach PR #99: **1.261 Tests**, davon **465 Native-Harness-Tests**.
Neue Tests verwenden ausschließlich den isolierten Native-Harness mit fester
FakePump und verbotenem realen HA-Service-Dispatch. Serviceversuche werden als
Event-Umschläge eingespeist, ohne produktive Services zu registrieren. Await-Races
verwenden deterministische `asyncio.Event`-Barrieren.

Lokale Validierung: Python 3.13.16, HA 2025.12.5, pytest 9.0.0, Ruff 0.17.0.
Die vollständigen pytest-Läufe erfolgten sequenziell mit
`LITELLM_LOCAL_MODEL_COST_MAP=True`; kein Zugriff auf reale HA-Geräte.

| Prüfung | Ergebnis |
| --- | --- |
| Neue Writer-Isolation-Tests | **35 bestanden**, 4,85 s |
| Gesamte Native-Harness-Suite | **500 bestanden**, 82,40 s |
| Vollständige pytest-Suite | **1.296 bestanden**, 138,07 s; keine Fehler oder Skips |
| Ruff (`custom_components tests scripts`) | Bestanden, Exit 0 |
| Formatprüfung (`custom_components tests scripts`) | Bestanden, 63 Python-Dateien, Exit 0 |
| Python-Kompilierung (`compileall`) | Bestanden, Exit 0 |
| Git-Whitespace einschließlich neuer Dateien (`diff --cached --check`) | Bestanden, Exit 0 |

Die neue Anzahl ist **1.261 + 35 = 1.296** bzw. **465 + 35 = 500**.
HACS und Hassfest werden über die unveränderten Repository-Workflows auf dem
PR-Branch ausgeführt; Remote-Ergebnisse werden nach Abschluss ergänzt.
