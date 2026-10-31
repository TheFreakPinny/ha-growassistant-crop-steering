# End-to-End Recovery Crash & Restart Harness (PR #107)

Der Gesamtharness liegt ausschließlich in
`tests/native_harness/recovery_crash_harness.py`; die Szenarien stehen in
`test_end_to_end_recovery_crash.py`. Keine produktive Datei wird verändert.
Ausgangspunkt ist `main` nach PR #106, Git-Tree
`645281a665973b9b752cf93ced7e549544c4fb23`.

## Aufbau und Simulationskontext

`RecoveryCrashHarness` komponiert die echten Pump-, Shot-, Cycle- und
Scheduler-Controller des vorhandenen `NativeHarness`, `_RecoveryLedger`, den
echten Reconciliation-Planner, `_RecoveryGateCoordinator`, Teilnehmerrevisionen,
`_RecoveryTransactionCoordinator` und `_CycleReleaseCoordinator`. Dazu kommen
der vorhandene `FakeGateway`, dessen Journal und Witness sowie der diagnostische
Transaktionsadapter mit Audit-Journal und unabhängig erhaltenem `AuditWitness`.
Es gibt keinen zweiten Recovery-Algorithmus und keine vereinfachten positiven
Teilnehmer oder Pläne.

Die Ausgangsstörung entsteht durch eine echte Shot-Ausführung ausschließlich
gegen den festen FakePump. Eine Ereignisbarriere hält die Rückkehr der bereits
bestätigten Fake-Aktivierung an; ein ausgefallener Sensor löst den echten
Abbruchpfad aus. Das Abort-Event bestimmt die Fortsetzung. Historische Operation
`interrupted`, mögliche Teilförderung und nicht erfolgtes Accounting stammen
damit aus den echten Controllern. Die einzige ON-Ausgabe gehört zur expliziten
Störungsvorbereitung. Nach Beginn der Recovery darf kein zusätzlicher
FakePump-Befehl entstehen. Reale HA-Serviceausgabe verbietet weiterhin der
Dispatcher-Spy der vorhandenen Fixture.

`stores()` und `persisted()` halten Journale und Witnesses getrennt von
disponiblen Instanzen. HA-Rekonstruktionen übernehmen ausschließlich den
Checkpoint, Ledger-, Protokoll-, Release- und Audit-Speicher; Besitznachweise
werden nicht wiederhergestellt. Alte Instanzen bleiben unter `retired` erreichbar.
Jeder `EventState` hält detached persistierte Zustände, unveränderliche
Teilnehmer-Snapshots, ihre Revisionsnachweise, HA-Boot, Gateway-Boot/Runtime,
Koordinator-Boot, Adapterepoche und Ledgerrevision fest. Auch Pump-, Shot-,
Accounting-, Cycle- und Scheduler-Sicherheitszustand bleiben dadurch prüfbar.

Die Instrumentierung umschließt vorhandene Collector-/Store-Schnittstellen.
Während der Erfassung wird der echte Source-Capture fertiggestellt und vor
Rückgabe angehalten; dessen eigener abschließender Konsistenzvergleich bleibt
aktiv. Der Gateway-Snapshot selbst ist synchron und wird nicht künstlich
innerhalb seiner atomaren Modellaufnahme unterbrochen.

## Deterministische Unterbrechungsgrenzen

`EventBarrier` hat ein Eintritts- und ein Freigabe-Event, feuert einmal und
verwendet weder Polling noch echte Wartezeiten. `parked` wartet gleichzeitig
auf den Eintritt und das Task-Ende, damit ein vorheriger Fehler sichtbar wird.
Jede Grenze prüft die Invarianten vor dem Anhalten und nach der Fortsetzung.

| Grenze | Tatsächliche Aufrufgrenze |
| --- | --- |
| Vor Gateway-Snapshot | `before_gateway_snapshot`, vor dem echten Integrationscollector |
| Nach Gateway-Snapshot | `after_gateway_snapshot`, vorhandenes `gateway_captured` |
| Während Teilnehmererfassung | `participant_collection`, Source-Capture vor Rückgabe |
| Nach Reconciliation-Plan | `after_plan`, unveränderter echter Plan mit Findings |
| Während Prepare | vorhandenes `prepare_captured` |
| Nach diagnostischer Prepare-Prüfung | `after_prepare` |
| Vor Audit-Commit | vorhandenes `commit_checked` |
| Während Audit-Commit | `audit_writing`, tatsächlicher asynchroner Store-Aufruf |
| Nach Audit-Persistenz vor Bestätigung | vorhandenes `commit_saved`; zusätzlich Store-Barrier nach Persistenz |
| Nach Commit-Bestätigung | `after_commit` |
| Während Gateway-Neustart | `gateway_restarting`, neue Boot-Epoche, vor Anbindung an den Collector |
| Während HA-Neustart | `ha_restarting`, nach Stop, vor Neukonstruktion |
| Während Ledger-Persistenz | `ledger_writing`, echter Ledger-Writer |
| Während Teilnehmerpersistenz | `participant_writing`, echter versionierter Writer |

Abbruch einer Coroutine beweist nicht, dass deren geschützter Storage-Task
abgebrochen wurde. Die Szenarien geben überlebende Schreibaufträge explizit frei
und warten auf deren Ende. Ungewissheitslatches bleiben erhalten; kein Retry
und keine Adoption einer späten Bestätigung findet statt. Der synchrone
Gateway-Konstruktor hat keinen internen await-Punkt: ein Crash mitten in dessen
Python-Anweisungen wird hier nicht behauptet.

## Restart-Reihenfolgen und Fencing

Getestet werden Gateway allein, Transaktionskoordinator allein, Audit-Adapter
allein, HA allein, Gateway vor HA, HA vor Gateway, drei Gateway-Neustarts,
drei vollständige HA-Neukonstruktionen und eine kombinierte Reihenfolge aus
Adapter, Koordinator, Gateway und HA. Jede neue HA-Instanz hat eigene Owners,
Listener, Config-Entries und Boot-Epochen. Alte Prepare-Referenzen bleiben
ungültig. Alte und neue Gateway-Instanzen weisen den alten ON-Intent sowohl
bei Annahme als auch bei Ausführung zurück, wenn der Gateway neu gestartet wurde.

Weitere acht Fälle kombinieren einen ausstehenden Audit-Storage-Callback mit
Gateway-, Adapter-, Koordinator- oder HA-Neustart jeweils vor oder nach der
Persistenz. Die alte HA-Serialisierung bleibt belegt, solange ihr Writer läuft.
Ein neues HA kann diese prozesslokale Sperre nicht als dauerhafte Sperre erben:
ein unbestätigter Receipt kollidiert stattdessen mit dem erhaltenen Witness.
Kein alter Callback kann einen pensionierten Adapter erneut autorisieren.

Explizite HA-Schließung darf zusätzliche OFF-Kommandos erzeugen. Sie wird als
separater Lebenszyklusschritt erfasst und darf niemals ON erzeugen. Die
Diagnostik selbst erhält keine Ausnahme für Pumpenbefehle.

## Multi-Store-Fehler und Ergebnisklassen

Gezielte Injektion betrifft Gateway-, Ledger-, Pump-, Shot-, Cycle-,
Transaktions- und Audit-Journal. `StoreFault` hält den vorhandenen MemoryStore
und dessen versionierte Persistenzschnittstelle bei. Vor-/Nach-Persistenzfehler,
Teilpersistenz und verspätete Bestätigungen werden getrennt geprüft.
Teilnehmer-Schreibproben übergeben dem echten Revisionswriter eine detached
Störprojektion mit zusätzlichem Probe-Feld. Die live gehaltenen historischen
Shot-Operationen und Accounting-Werte werden dabei niemals editiert. Ein
unveränderter Controller-Save wäre wegen der echten Digest-Deduplizierung kein
Schreibvorgang und damit kein gültiger Persistenzfehlertest.

Weitere Fälle verändern Schema, ältere Teilnehmerrevision mit neu berechnetem
gültigem Metadatenhash, Ledger-/Protokollprojektion, gespeicherte Audit-Historie
oder entfernen Payload und Revisionsenvelope. Gateway-Fehler nutzen dessen
eigene Before-/After-/Delay-Injektion, Schema, Rollback, verlorenen Witness und
ein abgeschnittenes Journal. Audit-Witness-Verlust wird separat geprüft.
Die gemeinsame Rollback-Prüfung setzt mehrere zuvor geänderte Stores zurück.

| Klasse | Beobachtung und Konsequenz |
| --- | --- |
| Erkannter Fehler | Schema-/Revisions-/Projektionsabweichung, Witness-Konflikt oder obsoleter Kontext: Konsistenz verweigert bzw. explizite Exception |
| Ungewisses Ergebnis | Schreiben wurde eingereicht, aber Fehler, Abbruch oder verspätetes Ack lassen den Commit nicht verlässlich bestätigen: kein Retry, alte Sitzung bleibt verbraucht |
| Nicht nachweisbare Vertrauensverletzung | Audit und Witness gemeinsam auf gültigen alten Präfix zurückgesetzt: historische Hashintegrität kann bestehen; Authentizität, aktuelle Autorisierung und Multi-Store-Atomizität bleiben ausdrücklich unbewiesen |

Das echte technische Recovery-Protokoll lehnt den echten Plan mit `plan_blocked`
ab. Es gibt somit keinen legitim erreichbaren produktiven Prepare-/Commit-Write
dieses Protokolls in dieser Kette. Dessen Fehlerfälle injizieren **ausschließlich
am Store**, ohne Events, positive Pläne oder Übergänge zu erfinden. Ein externer
Write, der vor jeder Änderung fehlschlägt, ist dem lesenden Protokollvergleich
nicht sichtbar; auch die Entfernung eines ohnehin leeren Protokollstores ist
kein nachweisbarer Rollback. Diese beiden Fälle dürfen diagnostisch konsistent
bleiben, behalten aber sämtliche Freigabesperren. Tests der eigentlichen
Protokollzustandsmaschine bleiben in der bestehenden Transaktionssuite.

Ein gemeinsamer Rollback sämtlicher Daten und aller unabhängigen Witnesses über
einen Prozessverlust hinweg ist grundsätzlich nicht zuverlässig erkennbar. Das
Modell liefert weder eine dauerhafte Vertrauenswurzel noch atomare Commits über
mehrere Stores. Selbst ein lokal bestätigter Audit-Receipt bleibt historisch.

## Kombinierte Races

Die Szenarien kombinieren Safety-OFF mit Prepare und Commit, Lease-Entzug mit
Source-Erfassung, Gateway-Neukonstruktion nach Gateway-Snapshot, vollständige
HA-Neukonstruktion nach Prepare-Teilnehmeraufnahme, echte Ledger-Anerkennung nach
Plan-Erstellung, neue Teilnehmerrevision nach Prepare und Gateway-Neustart nach
Audit-Persistenz. Ein zweiter Request derselben Ressource während des
Commit-Checks wird abgewiesen. Eine verspätete Gateway-Bestätigung nach
diagnostischem Recovery-Commit bleibt historische Evidenz und beseitigt dessen
Ungewissheitssperre nicht. Die FakeClock schreitet nur explizit fort; alle
Reihenfolgen ergeben sich aus Barrieren, nicht aus Event-Loop-Geschwindigkeit.

## Globale Sicherheitsinvarianten

Nach jedem modellierten Ereignis prüft `record`:

- Die echten Recovery-/Ledger-/Protokoll-/Release-Controller erlauben keine
  Native-Aktivierung; der Release-Koordinator bleibt `locked`, ohne Permit.
- Alle veröffentlichten Diagnosen behalten `native_activation_allowed=False`,
  `coordination_ready=False`, `hardware_exclusivity_proven=False`,
  `recovery_release_allowed=False`, `cycle_release_allowed=False` und
  `release_permit=None` sowie sämtliche ursprünglichen Blocker.
- Die tatsächliche Shot-Recovery bleibt gesperrt, Ersatzshots bleiben blockiert,
  der Scheduler läuft nicht und historische Operationen einschließlich
  Accounting bleiben identisch. Auch alte Runtime-Instanzen geben keine neuen
  Befehle aus.
- Das technische Protokoll hält keine Prepared-Session. Ein vollständiger
  Diagnose-/Audit-Durchlauf verändert ausschließlich Audit und AuditWitness;
  der gesonderte explizite Release-Versuch erzeugt höchstens eine negative
  Release-Quittung. Reale Prepare- und Release-Versuche bleiben blockiert.

Der End-to-End-Normalfall prüft zusätzlich Duplicate-/Replacement-Ablehnung und
unverändertes Accounting über die bestehenden realen Shot-APIs. Es gibt keinen
Gate-Resolver-Aufruf, automatischen Replay oder künstlich entfernte Findings.
Die fünf Diagnoseflags sind Aussagen des diagnostischen Vertrags; physische
Eigenschaften werden daraus ausdrücklich nicht abgeleitet.

## Timing-Beobachtungen zu PR #106

Der bekannte Test
`tests/shot_engine/test_native_safety.py::test_delivery_diagnostics_keep_attempts_separate_from_successes`
enthält eine nominal erfolgreiche Shot-Ausführung mit echtem monotonic Timing.
`_supervise` überwacht mit `MONITOR_INTERVAL=0.01` die Frist aus Aktivierungszeit
plus Shot-Dauer plus Monitorintervall. Bei verzögerter Event-Loop-Ausführung kann
die Supervisor-Fortsetzung die reguläre Abschlussfortsetzung überholen und
`deadline_exceeded` auslösen. Das ist eine Scheduling-Abhängigkeit, kein Beweis
für eine sichere Fristüberschreitung oder einen Fehler der neuen Recovery-Kette.

Die neue Störungsvorbereitung wartet auf Aktivierung und Abort-Event, niemals
auf die nominale Shot-Dauer. Recovery-Fälle haben keine laufenden Shots. Keine
neue Synchronisierung verwendet `sleep`, Timer, Polling oder verlängerte
Produktionsdeadlines. Das Ereignisprotokoll kopiert kleine In-Memory-Snapshots
synchron; sein Aufwand wächst mit der Historie. Unbegrenzte Journalgrößen und
Worst-Case-Loop-Latenzen sind dadurch nicht nachgewiesen. Die lokalen Suiten
werden nacheinander ausgeführt, ohne konkurrierende pytest-Prozesse. Produktive
Timeouts und Sicherheitsprüfungen bleiben unverändert.

## Modellgrenzen und nächste Phase

Nicht nachgewiesen sind physisches OFF, Relaiszustand, reale Pumpenexklusivität,
externe Writer-Sperren, gemessene Fördermenge, reale Pumpenidentität,
Dateisystem-/fsync-Atomizität, Prozess-/Thread-übergreifende Serialisierung und
Fencing über einen vollständigen Verlust aller Vertrauenszustände. Auch die
MemoryStore-Teilpersistenz ist eine gezielte fehlerhafte Projektion, kein Modell
von Dateisystemsektoren oder tatsächlichen Stromausfällen. HA-Neustarts
rekonstruieren reale Softwarecontroller in isolierten Fixtures, keinen Rechner.
Ein isolierter HA-Neustart informiert einen unabhängigen Gateway nicht von
selbst über den neuen Boot. Die Ablehnung alter Diagnoseberechtigung ist damit
geprüft; eine sofortige externe Writer-Sperre vor Ablauf einer noch laufenden
Gateway-Lease ist dadurch nicht nachgewiesen. Die ON-Fencing-Prüfung bindet sich
explizit an den Gateway-Neustart und dessen eigene Runtime-/Boot-Fences.

Vor einer nächsten Freigabephase braucht es unabhängige dauerhafte
Vertrauensnachweise, physische OFF-/Exklusivitätsverträge, authentische
Ressourcen-/Writeridentität, Operation-/Cycle-Provenienz, geklärte Teilförderung,
Teilnehmer-eigene Gate-Commit-/Revokationsverträge und eine konservative
Multi-Store-Commit-/Rollback-Strategie. Erst diese Nachweise dürfen die
unveränderten echten Plan- und Release-Voraussetzungen erfüllen. Fake-Receipts
reichen dafür nicht aus.

## Validierung

Baseline: **1.702 Tests**, davon **906 Native-Harness-Tests**. Die neue Datei
enthält **99 zusätzliche Tests**; erwarteter Gesamtumfang: **1.801 Tests**,
davon **1.005 Native-Harness-Tests**. Python 3.13.16, Home Assistant 2025.12.5,
HA-Fixtures 0.13.301 und Ruff 0.17.0.

| Neue Szenariogruppe | Fälle |
| --- | ---: |
| Vollständige lesende Diagnose-/Audit-Kette | 1 |
| Crash an jeder Unterbrechungsgrenze | 14 |
| Restart-Reihenfolgen mit Neukonstruktion | 9 |
| Restart bei ausstehendem Audit-Callback | 8 |
| Schreibfehler vor/nach Persistenz und Teilpersistenz, sechs Stores | 18 |
| Schema-, Revisions- und Rollback-Schäden, sechs Stores | 18 |
| Gateway-Journalfehler | 7 |
| Kombinierte Snapshot-/Prepare-/Commit-Races | 8 |
| Zwei Requests und verspätetes Gateway-Ack | 1 |
| Gemeinsamer Rollback mit/ohne Witness-Rollback | 2 |
| Späte Bestätigung in fünf weiteren Stores | 10 |
| Echte technische Prepare-Ablehnung | 1 |
| Echte Release-Ablehnung | 1 |
| Audit-Witness-Verlust | 1 |
| **Gesamt** | **99** |

| Prüfung | Ergebnis |
| --- | --- |
| Neue End-to-End-Tests, abschließender Lauf | 99 bestanden (24,26 s) |
| Gesamte Native-Harness-Suite | 1.005 bestanden (166,55 s); danach erneuter Lauf aller 99 End-to-End-Fälle nach Präzisierung der Erfassungs-/Restart-Grenzen |
| Vollständige pytest-Suite am finalen Python-Stand | 1.801 bestanden (219,47 s), keine Fehler/Skips |
| Bekannter Deadline-Test zusätzlich isoliert | 1 bestanden (1,31 s) |
| Profilierter normaler End-to-End-Durchlauf | 1 bestanden (1,64 s) |
| Ruff | Bestanden, Exit 0 |
| Formatprüfung | Bestanden, 74 Dateien, Exit 0 |
| Python-Kompilierung | Bestanden (`custom_components`, `tests`, `scripts`), Exit 0 |
| Git-Whitespace-Prüfung | Bestanden, Exit 0 |
| HACS am Implementierungscommit `0f882b3` | [Bestanden](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/41029786902) |
| Hassfest am Implementierungscommit `0f882b3` | [Bestanden](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/41029787093) |

Der folgende Dokumentationscommit ergänzt ausschließlich diese Ergebnisse.
Alle lokalen pytest-Prozesse liefen nacheinander. Im vollständigen und im
isolierten Lauf trat der bekannte `deadline_exceeded`-Fehler nicht auf.
Ein zusätzlicher cProfile-Lauf des normalen End-to-End-Falls maß für `record`
26 Aufrufe mit insgesamt 0,121602 s einschließlich Unteraufrufen, im Mittel
4,677 ms unter Profilierungsinstrumentierung. Das ist eine Beobachtung für die
kleine Fixture-Historie, keine obere Latenzgrenze. Während dieser Aufnahmen läuft
kein Shot; ein zusätzlicher Deadline-Konflikt durch diese Orchestrierung wurde
nicht beobachtet. Die mögliche Scheduling-Race des bestehenden erfolgreichen
Shot-Tests bleibt außerhalb dieser rein diagnostischen Implementierung.
