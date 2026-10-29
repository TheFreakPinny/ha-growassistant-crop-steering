# Isolierte Fake-Gateway-/Recovery-Diagnose (PR #104)

Aufbauend auf PR #103 liegt die gesamte Integration in
`tests/native_harness/fake_gateway_recovery.py`. Ausschließlich der Fake erhält
`evidence_snapshot()` als lesenden Einstieg. Kein produktiver HA-Setup-, Service-,
Pumpen-, Shot-, Cycle- oder Scheduler-Pfad importiert diese Integration.

## Gemeinsame Evidenz und Identität

`GatewayEvidence` ist ein eingefrorener, von den veränderlichen Journalcontainern
abgelöster Snapshot. Er enthält die vollständige `ActuatorIdentity` inklusive
`ResourceBinding`, Hardware-ID, Kanal und Enforcement-Domain, Gateway-Boot,
Instanz-/Speicherlaufzeit, Ownership-Generation, Lease, Owner und Lease-Status,
Stop-Latch/Fehler/Fencing-Serien, Journalrevision und Schema, alle Commands und
Safety-Stop-Receipts, Witness-Präsenz/-Latch/-Übereinstimmung, Fake-Ausgang,
letzte Safety-OFF-Evidenz, offene/ungewisse Commands, verspätete Bestätigungen,
Sequenzstände, Heartbeat/Watchdog und Prüfzeitpunkt. Die Fake-Uhr erhält eine
prozesslokale Zeitbasis-ID; Ticks werden niemals mit HA-UTC verglichen.

Der synchrone Leser ruft weder `poll()` noch `check()`/`lookup()` auf: Diese
bestehenden Gateway-Methoden können Fehler latchen oder Safety-OFF auslösen.
Stattdessen vergleicht er Journal und unabhängig erhaltenen Witness direkt.
Ein fehlender/inkonsistenter Witness bleibt blockierend, ohne ihn zu vergiften.
Unlesbare oder falsch geformte Journale ergeben unvollständige Evidenz.
Der Leser erteilt keine Lease, startet kein Recheck und führt kein ON/OFF aus.

`DiagnosticBinding.capture()` bindet zwei tatsächlich gelesene Nachweisbereiche:
Gateway-Snapshot und bestehende `ParticipantAggregation`. Das ist eine explizite
**Modellreferenz**, kein neuer vertrauenswürdiger Ressourcenbeweis. Identische
HA-Entity-IDs beweisen keine identische physische Pumpe. Die Gateway-Identität
muss vollständig mit der Referenz übereinstimmen; Config Entry und Pumpen-Entity
müssen zusätzlich zur Recovery-Anfrage passen. Die Modell-Lease benennt als Owner
die tatsächliche Pumpen-Controller-Epoche und denselben HA-Boot.

Gateway-Ownership und lokale HA-Ownership sind unabhängige Zähler. Beide bleiben
getrennt gebunden: Gateway-Boot/Generation/Lease sowie sämtliche tatsächlichen
Teilnehmer-Runtime-Epochen mit Boot, Controller, Ressource, Ownership- und
Safety-Generation. Es wird weder eine Gleichheit unabhängiger Zähler behauptet
noch eine Teilnehmergeneration für einen positiven Test umgeschrieben.

## Revisions-, Cycle- und Operationsbindung

Die Referenz hält vollständigen Cycle-/Operationsscope einschließlich
Operationsdigest, Review und Disposition sowie alle Teilnehmer-/Ledgerrevisionen,
Journalidentitäten, Receipts und Runtime-Epochen. Pro Anfrage neue Attempt-IDs und
Aufnahmezeiten sind aus dem Referenzvergleich ausgenommen; ihre Gültigkeit prüft
weiterhin der bestehende Recovery-Koordinator. Alle Teilnehmernachweise werden
zusätzlich über `aggregate_evidence()` geprüft. Dessen ursprüngliche Blocker
werden ausschließlich vereinigt, niemals entfernt.

`CommandIntent` enthält bisher weder Cycle-ID noch Operations-ID. Auch ein
abgeschlossener Fake-Command beweist daher keine Zuordnung zu einem historischen
Shot. Bei vorhandenem Command-Journal bleibt
`gateway_operation_provenance_unproven` blockierend. Ein leeres Command-Journal
kann diagnostisch widerspruchsfrei zu einem gebundenen Recovery-Scope sein; es
beweist weder, dass ein unterbrochener Shot nie gelaufen ist, noch dessen
Fördermenge, Disposition oder physische Pumpenidentität.

## Snapshot-Konsistenz, Races und Restarts

`NativeGatewayRecoveryCoordinator.async_check()` liest zunächst das Gateway,
ruft die bestehende rein diagnostische Teilnehmerprüfung auf und liest das
Gateway unmittelbar vor Veröffentlichung erneut. Deterministische Testbarrieren
liegen an `gateway_captured`, `recovery_captured` und `before_publish`. Nach der
letzten Barriere gibt es kein `await` zwischen abschließender Revalidierung und
Veröffentlichung. Erfassung selbst ist synchron im kooperativen Einzel-Eventloop;
Threads oder echte verteilte Transaktionen werden nicht simuliert.

Alle Snapshotwerte außer fortschreitender Aufnahmezeit müssen unverändert sein.
Zeitbasis, Lease-Ende, Watchdog und monotone Zeitordnung werden separat geprüft.
Boot, Ownership-Wechsel, Safety-OFF, Journaländerung, Rollback und Witness-Verlust
machen das Ergebnis blockierend. Die bereits vorhandene HA-Koordination prüft
zusätzlich aktuelle Owner, Revisionen, Runtime-Ereignisse und Teilnehmernachweise.
Ein HA-Neustart oder ein neuer Controller kann alte Referenzen nicht übernehmen.
Ein Gateway-Neustart macht die vorherige Instanz durch den Speicherlaufzeitzähler
obsolet, auch bei fehlgeschlagenem Boot-Commit.

Safety-OFF verändert den Stop-Serial auch dann, wenn der Ausgang bereits OFF ist;
Rollback stellt diesen unabhängigen Fence nicht zurück. Der Witness erkennt ein
altes Journal nur unter der aus PR #103 übernommenen Annahme seiner unabhängigen
Erhaltung. Ungewisse Commits und verspätete Acknowledgements repariert die Diagnose
nicht. Offene Ausführungen werden weder abgeschlossen noch wiederholt.

`diagnostic_current()` akzeptiert nur das zuletzt von dieser Instanz ausgegebene
widerspruchsfreie Ergebnis, prüft beide Nachweisbereiche erneut und verwirft bei
Invalidierung seine eigene transiente Referenz. Gleichwertige Kopien und ältere
Anfragen sind nicht aktuell. Parallele Anfragen derselben Instanz werden abgelehnt;
Abbruch installiert kein Ergebnis. Mehrere Wrapper teilen weiterhin die exklusive
und serialisierte bestehende Recovery-Koordination. Es gibt keine persistente
Integrationsbestätigung und keinen automatischen Retry.

## Diagnostische Ergebnisse und unverändert blockierte Gates

| Status | Bedeutung |
| --- | --- |
| `gateway_evidence_complete` | Gateway-Snapshot konnte vollständig erfasst werden |
| `gateway_internally_consistent` | Beide Gateway-Lesungen unverändert und ohne interne Befunde |
| `recovery_participants_complete` | Alle fünf tatsächlichen Teilnehmer vorhanden |
| `evidence_conflict_free` | Modellreferenz, Teilnehmerrevisionen/-Scope und aktuelle Nachweise widersprechen sich nicht |
| `hardware_exclusivity_proven` | Immer `False` |
| `recovery_release_allowed` / `cycle_release_allowed` | Immer `False` |
| `native_activation_allowed` / `coordination_ready` | Immer `False` |
| `release_permit` | Immer `None` |

Die ersten vier Zustände sind Erfassungs-/Konsistenzdiagnostik und keine
Autorisierung. Auch bei vier positiven Diagnosestati bleiben mindestens
`partial_delivery_unresolved`, `cycle_disposition_unproven`,
`external_writers_unproven`, `ownership_epoch_unproven` und
`coordinated_resolution_unimplemented` erhalten. Diese konservative Untergrenze
wird niemals durch ein Fake-Receipt aufgehoben. Weitere Teilnehmerblocker und
historische Fehler bleiben sichtbar.

Der Wrapper verändert ausschließlich seinen eigenen Busy-/Ergebniszustand.
Er schreibt keine Journale, setzt keine Recovery-Reasons oder `_blocked` zurück,
ändert kein Accounting, erzeugt keinen produktiven Release-Permit und startet
keinen Scheduler. Kein Replay, Retry oder Ersatzshot wird ausgelöst. Er greift
weder auf den produktiven Release-Koordinator noch auf dessen Permit-Ausgabe zu.

## Modellgrenzen und nächste Voraussetzungen

Der feste In-Memory-Aktor besitzt keinen Hardwareadapter und keine Netzwerkausgabe.
Prozesslokale Objektidentitäten und eine deklarierte Hardware-ID sind keine
Hardwareauthentifizierung. Ein gemeinsamer Rollback aller vertrauenswürdigen
Zustände bleibt unnachweisbar; Python-Objekte schützen nicht gegen bösartigen
Code im selben Prozess. Die Beobachtung öffnet keine Gates, auch wenn Daten
konsistent aussehen.

Eine spätere koordinierte Auflösung benötigt unabhängig belegbare physische
Ressourcenidentität/Exklusivität, Ausschluss aller externen Writer, echte
OFF-/Ownership-Nachweise, eine rollbackfeste Vertrauensbasis, eine eindeutige
Cycle-/Operationsbindung jedes Commands, belastbare Behandlung ungewisser
Förderung, Teilnehmer-Commit-/Fencing-Protokoll über Restarts und einen explizit
geprüften Cycle-/Accounting-/Baseline-Übergang. Diese Garantien liefert PR #104
nicht. Bestehende Gates bleiben bis dahin geschlossen.

## Validierung

Baseline nach PR #103: 1.466 Tests, davon 670 Native-Harness. **71 zusätzliche
Integrationstests**, ausschließlich diagnostische positive Aussagen.

Validiert mit Python 3.13.16, Home Assistant 2025.12.5 und dem bestehenden
Fixture-Pin 0.13.301:

| Prüfung | Ergebnis |
| --- | --- |
| Neue Integrationstests | 71 bestanden (11,22 s) |
| Gesamte Native-Harness-Suite | 741 bestanden (91,81 s) |
| Vollständige pytest-Suite | 1.537 bestanden (144,31 s), keine Fehler/Skips |
| Ruff | Bestanden |
| Formatprüfung | Bestanden, 69 Dateien |
| Python-Kompilierung | Bestanden |
| Git-Whitespace-Prüfung | Bestanden |
| HACS / Hassfest | GitHub-Actions-Validierung am zugehörigen PR; Ergebnisse und Logs in dessen Checks |

Die Tests verwenden die tatsächlichen Recovery-Teilnehmer des Native-Harness.
Echte HA-Serviceausgabe bleibt durch dessen verbietenden Dispatcher-Spy gesperrt.
Die HA-Fixtures benötigen in dieser Cloud-Sandbox den Netzwerkmodus mit aktivem
Loopback; es wird keine reale Hardware oder produktive HA-Instanz kontaktiert.
Unveränderte Checkpoints, Journal-/Witness-Werte, Accounting und Schedulerzustände
sowie explizite verbotene Methodenaufrufe prüfen die Lesegarantie.
Die drei deterministischen Prüfgrenzen werden jeweils gegen 14 verschiedene
Gateway-/HA-/Speicher-/Zeitänderungen getestet. Weitere Tests decken echte
HA-Wiederherstellung, laufende Ausführungen, verspätete Bestätigungen,
Teilnehmerwidersprüche, vollständige Ressourcenidentität und unveränderliche
Snapshots ab.
