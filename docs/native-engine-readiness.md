# Native Engine: Integrationsreife nach PR #108

Analyse vom 31.10.2026 auf `main` **e0bc3316c0bfe78958a0f98c5357eea9d2419c3f**
(PR #108 gemergt). Dieser PR ergänzt ausschließlich die drei Planungsdokumente.
Keine Aktivierung, kein Schedulerstart, keine Pumpenbefehle, keine Änderung von
Shot-Historie, Recovery-Sperren oder Schutzmechanismen.

## Ergebnis und Nachweisgrenzen

Der funktionale Kern existiert bereits. Es fehlen vor allem seine produktive
Verdrahtung, eine kontrollierte Übernahme vom Blueprint und ein bedienbarer,
konservativer Neustart-/Fehlerquittierungsweg. Pump-, Shot-, Cycle- und
Schedulerlogik neu zu schreiben wäre unnötig. Ein externer Gateway oder ein neuer
Hardwareprototyp ist kein Arbeitspaket dieser Roadmap.

`CODE` bezeichnet den gelesenen Code, `TEST` einen konkreten lokalen Test mit
simulierten Pumpen; `OFFEN` eine noch zu implementierende oder im Zielbetrieb zu
validierende Eigenschaft. Erfolgreiche Tests sind keine Betriebs- oder
Hardwarefreigabe. Die [Paritätsmatrix](native-engine-feature-parity.md) trennt
Kernfunktion und produktive Anbindung; die
[Roadmap](native-engine-integration-roadmap.md) nennt kleine Folge-PRs.

Der [Audit aus PR #108](native-recovery-safety-audit.md) bleibt als Nachweis der
Grenzen erhalten. Seine Empfehlungen für allgemeine physische Isolation werden
hier nicht als notwendige nächste Integrationsarbeiten übernommen. Ein späterer
Aktivierungs-PR muss diesen engeren HA-Betriebsumfang ausdrücklich dokumentieren
und prüfen; bestehende harte Sperren dürfen dafür weder stillschweigend entfernt
noch durch `simulated_pump=True` umgangen werden.

## Komponentenbestand

Alle folgenden Pythonpfade beziehen sich auf
[`custom_components/growassistant_crop_steering`](../custom_components/growassistant_crop_steering/).

| Komponente | Bereits vorhanden und belegbar | Tatsächlich offen |
| --- | --- | --- |
| `pump.py`: `PumpController` | CODE/TEST: HA-weiter Native-Owner, Laufzeitvalidierung bis 300 s, Intent vor ON, Service-/Bestätigungstimeouts, frische HA-OFF-Bestätigung, Stop/Cancel, spätes ON wird erneut OFF reconciliert, ursprüngliches Ziel bleibt erhalten. `tests/pump/test_pump.py`: `test_late_activation_cannot_be_cleared_by_earlier_off`, `test_restart_reconciles_recorded_target`, `test_multiple_owners_rejected`. | Kein Produktionsowner; Registry sperrt keinen Blueprint. Initialisierung kann bei Recovery bereits OFF senden: keine unbedachte Konstruktion/Initialisierung als reine Diagnose. |
| `shot_engine.py`: `_ShotCoordinator` | CODE/TEST: atomare lokale Reservation, Operations-ID gegen Wiederverwendung, frische Entscheidung vor ON, Safety-Supervision, begrenzte Laufzeit, OFF vor Erfolgsbuchung, Accounting-Lock mit Reset, kein Replay. `tests/shot_engine/test_shot_engine.py`: `test_success`, `test_concurrent_and_cancellation`, `test_restart_requires_review_no_accounting_replay`. | Öffentlicher Bedien-/Recoverypfad und Lifecycle-Anbindung. Options plus Store sind keine gemeinsame Transaktion. |
| `cycle.py`: `_NativeCycleController` | CODE/TEST: P0/P1/P2/P3, Start/Completion/Referenz, Sunrise-ID, Reset-/Accounting-Serialisierung, Drift- und Restart-Sperren. `tests/shot_engine/test_cycle.py`: `test_sensor_transitions_and_cycle_reference`, `test_scheduler_sole_timer_manages_phases`. | Kontrollierte Erstübernahme und Restart-Fortsetzung. Verlangt explizit saubere managed Werte, übernimmt keine Legacy-Autorität; Sunrise mehr als 1 s verpasst blockiert. |
| `scheduler.py`: `_NativeScheduler` | CODE/TEST: gefilterte Events, Optionslistener, Koaleszierung, ein UTC-Timer, keine Shot-Queue, kein Catch-up, Fehler-Retrylatch, geordnetes Stop/Close. `tests/shot_engine/test_scheduler.py`: `test_bursts_and_concurrent_requests_never_queue`, `test_lifecycle_drain_active_and_cleanup`. | Separat autorisierter produktiver Start und Neustartzulassung. `async_start()` fordert sofort Evaluation an und ist deshalb keine passive Initialisierung. |
| `irrigation.py` | CODE/TEST: immutable Inputs, Options-vor-Data, Helperauflösung, Phasen/Soak/VWC/Drain/Budgets, Fail-closed bei ungültigem Schedule. Tests unter `tests/test_irrigation.py`. | Kein neuer Regelkern nötig. Vergleich mit bisherigen Sensorentscheidungen und dokumentierten Abweichungen abschließen. |
| `native_safety.py` | CODE/TEST: Alter, Quorum, Spread, Korruption/restored Werte; Recoverykategorien und Ownershipcheck. `tests/shot_engine/test_native_safety.py`. | Policy bislang privat: 300 s, mindestens 1 und 100 % Sensoren, maximal 10 Prozentpunkte Spread. Geeignete Standortwerte und HA-Konfigurationsweg validieren. Ownership akzeptiert ausschließlich Simulation; Migration bleibt immer gesperrt. |
| `native_diagnostics.py` | CODE/TEST: bereits im normalen Setup aktiv, Live-Sensorassessment, streng gelesene historische Stores, stabile Reason-Attribute, Listenercleanup. `tests/lifecycle/test_native_diagnostics.py`. | Live-Runtimezustand fehlt: Status ist absichtlich `disabled`, Recovery `not_evaluated`, Ownership `unverified`. Grüne Sensorreadiness ist keine Aktivierungsfreigabe. |
| Config-/Options Flow, `config.py` | CODE/TEST: Pumpen-/Sensor-/Lichthelper-Zuordnung, mehrere VWC-Sensoren, P1/P2 manual/sensor, leere optionale Overrides, Singleton. `tests/lifecycle/test_lifecycle.py`: Userflow und Optionsflow/Reload. | Kein Executorwechsel, kein Native-Start, keine Quittierung. Numerische Parameter existieren als Number-Entitäten; nicht nochmals in einem konkurrierenden Parameterbestand speichern. |
| Entitäten und Services | CODE/TEST: fünf Plattformen (`binary_sensor`, `button`, `number`, `sensor`, `switch`); persistierte Parameter/Flags/Zähler, Dispatcher, bestehende Reset/Start-P1/Complete-P1/Last-Shot/Stop-Services. | Services bearbeiten derzeit Legacy/managed Zustand, keine Native-Cycle-Bedienung. Native-Modus braucht ownerabhängiges Routing und Schutz von Zählern/Flags gegen parallele Änderungen. |
| `__init__.py` Lifecycle | CODE/TEST: Setup lädt Diagnostics, Plattformen und bisherigen `_CycleResetCoordinator`; kein Native-Stack. Unload: Scheduler → Cycle → Shots → Pump → Plattformen; False bei unbestätigtem Shot-/Pump-Close. | Runtimecontainer, Fehlerrollback bei Teilinitialisierung, eindeutiger Resetowner, Wiederanbindung beim Reload. Unloadhooks sind vorhanden, ersetzen aber keine Setupintegration. |

## Recovery pragmatisch einordnen

| Kategorie | Bestehender Bestand | Entscheidung für erste HA-Version |
| --- | --- | --- |
| Notwendige Laufzeitlogik | Pumpenowner, Zielbindung, Zeitlimits, OFF-Reconciliation; Shotreservation und IDs; aktive Sensor-/Drainprüfung; Cycle-/Accounting-Locks; Schedulergeneration und Retrylatch. | Unverändert als Kern übernehmen. Keine Parallelsteuerung oder vereinfachte direkte ON-Services daneben. |
| Notwendige Neustart-/Fehlerbehandlung | Pump-Recoverytarget; Shot-/Cycle-Intents und Checkpoints; `native_safety.assess_recovery`; `recovery_revision.py` mit strengem Store-Envelope und bestätigten Revisionen. | Behalten. Unbekannte Stores, offene Writes, unklare Lieferung und Options-/Journalabweichung sperren. Neue Quittierung muss Ursache prüfen und eine neue Entscheidung zulassen, niemals den alten Shot wiederholen. |
| Optionale zusätzliche Diagnostik | `recovery_snapshot.py` und Snapshotmethoden; `recovery_writer.py` Eventbeobachter; `recovery_ledger.py`, `recovery_plan.py` Audit und Findings. | Vorhanden lassen. Snapshots/Revisionen sind bereits von Kernmodulen referenziert, daher nicht pauschal herauslösen. Kein zusätzlicher Writer-Monitor als Beweis exklusiver Kontrolle erforderlich. Ledger/Planner nicht allein für einen Setup-Erfolg initialisieren. |
| Entwicklungsprotokolle | `recovery_participant.py` Evidenz-/Gateaggregation, `recovery_transaction.py` technische Prepare-/Commit-Historie, `recovery_release.py` verweigernde Releasekette und Harnessautorität. | Keine unmittelbar notwendige positive Wiederaufnahme implementiert. Bestehende Kern-Evidenzmethoden behalten; keine produktive Pflichtinitialisierung dieser Protokollkette. Ein separater kleiner HA-Quittierungspfad muss die vorhandenen Kernlatches respektieren und eigenständig getestet werden. |
| Ausschließlich Test-/Forschungsmodelle | Fake-Gateway, Fake-Recovery/-Transaktionen, Crashharness unter `tests/native_harness`; `actuator_contract.py` liefert reine Vertragsvalidatoren. | Als Regressionsevidenz erhalten. Kein realer Transport, Gatewaybetrieb, Lease-/Witness-Dienst oder Hardwareprojekt ableiten. Validatoren sind kein realer Aktoradapter. |

Die Einordnung beschreibt Verantwortung, keine Löschliste. Insbesondere ist nicht
jede `recovery_*.py`-Datei ausschließlich Testcode. Ihr Import durch einen
Kerncontroller bedeutet wiederum keine produktive Aktivierung. Die beiden
Audit-Guards in `tests/native_harness/test_safety_audit_boundaries.py` schützen
Harnessimportgrenze und inaktiven Entry-Lifecycle.

## Konkrete Integrationshindernisse

1. **Steuerungs- und Resetverantwortung:** Native und Blueprint dürfen weder
   gleichzeitig noch während auslaufender YAML-Delays dieselbe Pumpe bedienen.
   Blueprint `mode: single` und Native-Owner schützen jeweils nur ihren eigenen
   Pfad. Auch der bestehende Sunrise-Reset würde Native-Ownership blockieren
   (`test_external_reset_blocks_native`). Ein expliziter Executorzustand muss
   Services, manuelle Stateänderungen und Reset durch denselben Owner führen.
2. **Start-/Stopverdrahtung:** Initialisierungsreihenfolge Pump → Shots → Cycle →
   Scheduler ist vorhanden und im Harness nutzbar. Pumpinitialisierung ist wegen
   Recovery-OFF erst nach kontrollierter Übernahme erlaubt. Scheduler bleibt bis
   zur gesonderten Startentscheidung gestoppt. Bei Teilfehlern rückwärts schließen;
   blockierten Owner bei unbestätigtem OFF nicht durch einen neuen ersetzen.
3. **Migration von Parametern und Zustand:** Inputs und Number-Entitäten
   wiederverwenden. Blueprintinputs können auf andere Helper zeigen als der
   Config Entry; deshalb eine konkrete Zuordnung vergleichen. Managed Optionen
   übersteuern Legacywerte. Zähler/Flags/Referenz sind nicht beliebig importierbar;
   bevorzugt Übergabe an einer sauberen Zyklusgrenze, echte Last-Shot-Historie und
   Soak behalten. Keine stillen Datenkorrekturen oder nachgeholten Shots.
4. **Neustart und Quittierung:** Cycle blockiert auch bei konsistentem Neustart,
   Scheduler nach Fehler, Shot bei Unsicherheit. Es gibt keinen produktiven
   Recovery-Clear-Service. Eine bloße neue Instanz oder Checkbox löst das nicht.
   HA-Neustart zunächst gesperrt; Pump-OFF am gespeicherten Ziel reconciliieren,
   Historie/Checkpoints/Sensoren prüfen, dann manuelle Freigabe einer neuen
   Evaluation. Unklare Lieferung bleibt unreplayed und als solche dokumentiert.
5. **Bedienung/Diagnose:** Native-P1-Start/-Completion an vorhandene Cyclemethoden
   anschließen; Notstop an koordiniertes Stop/Close statt nur direkten OFF-Aufruf.
   Last-Shot-/Counter-Services dürfen keinen aktiven Native-Checkpoint verändern.
   Live-Status, konkreter Sperrgrund und Quittierungsbedarf müssen sichtbar sein.

## Enges Sicherheitskonzept und Migration

Für den ersten kontrollierten HA-Betrieb reichen als Implementierungsziel:
eine lokale Steuerungsverantwortung, keine doppelten Reservationen/IDs, begrenzte
Shots, OFF bei Fehlern soweit über HA erreichbar, blockierende Sensorvalidierung,
keine automatischen Ersatzshots und manuell freizugebender Neustart. Kein
Exactly-once-Versprechen für mehrere Speicherdateien: bei Abweichung stoppen und
prüfen. Mögliches Unterzählen nach Crash ist ehrlicher als ein wiederholter Shot.

Vorgeschlagene spätere Übergabe: Native gesperrt halten → konkrete Blueprintinstanz
und weitere bekannte HA-Writer stoppen/deaktivieren → laufende Automation inklusive
normalem und verzögertem Failsafe-OFF sowie ausstehender Calls beenden → frisches
OFF und Parameter/Zustand prüfen → lokalen Native-Owner übernehmen → Recovery
prüfen → expliziter begrenzter Start. Nur Automationdisable genügt nicht als
Nachweis, dass ein laufender Delay beendet ist. Unbekannte Konfiguration oder
laufende Writer blockieren die Übergabe. Rollback spiegelbildlich: Native erst
quiescen/OFF bestätigen, Records erhalten, Zustand prüfen, dann allein Blueprint
freigeben. Kein automatischer Wechsel bei Fehlern.

HA-Zustand bestätigt weder elektrisches OFF noch Wasserlieferung. Ein gestörter
HA-Loop, Gerätedefekt, verlorene Verbindung und Writer außerhalb der geprüften
HA-Konfiguration bleiben dokumentierte Grenzen. HA-Serviceevents beweisen keine
externe Writer-Isolation. Bestehende Hardwarehinweise bleiben erhalten; deren
technische Weiterentwicklung ist kein Bestandteil dieser Integrations-Roadmap.

## Validierung dieses Analyse-PRs

Lokal auf Python **3.13.16**, HA **2025.12.5**, Fixture **0.13.301**:

| Prüfung | Aufruf | Ergebnis |
| --- | --- | --- |
| Native-Harness | `LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q tests/native_harness` | 1.007 bestanden, 150,71 s, Exit 0. |
| Vollständiges pytest | `LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest -q` | 1.803 bestanden, 199,93 s, keine Fehler/Skips, Exit 0. |
| Ruff | `python -m ruff check custom_components tests scripts` | bestanden, Exit 0. |
| Format | `python -m ruff format --check custom_components tests scripts` | 75 Dateien unverändert formatiert, Exit 0. |
| Kompilierung | `python -m compileall -q custom_components tests scripts` | bestanden, Exit 0; Pycache außerhalb des Quellbaums. |
| Whitespace | `git diff --check` | bestanden, Exit 0; nach Staging erneut gegen `origin/main` geprüft. |
| HACS / Hassfest | bestehende GitHub-PR-Workflows | Status und Runlinks im PR; keine lokale Simulation dieser Actions. |

Der erste Harnessaufruf brach vor der Collection wegen fehlendem Fixturepaket
ab; nach dessen Installation fehlte noch `freezegun`. Daraufhin wurden die
bestehenden gepinnten `.github/requirements-ci.txt` in der Testumgebung ergänzt.
Keine Quell-, Test- oder Dependencydatei wurde dafür geändert. Ausgeführt wurde
mit dem dokumentierten HA-Fixture-Metadata-Ausnahmefall (Fixture verlangt
2025.12.4, geprüft wird 2025.12.5); siehe
[Fixture-Kompatibilität](../tests/lifecycle/README.md#fixture-dependency-compatibility).
Fetch/Installation und HA-Fixtures nutzten die unterstützte Netzwerkberechtigung
bei unverändertem Proxy; keine Produktions-HA-Verbindung. Die anfänglich fehlende
Proxyberechtigung und der UV-Cachepfad wurden nur für die Arbeitsumgebung korrigiert.

Die Baseline nach PR #108 bleibt 1.803 Tests; diese Zahl ist kein Paritätsbeweis.
Zielhardware, produktive HA-Installation, tatsächliche Blueprintabschaltung und
Standortparameter bleiben OFFEN. Es werden keine neuen Tests für unveränderten
Laufzeitcode hinzugefügt; vorhandene Tests prüfen die dokumentierte Ausgangslage.
