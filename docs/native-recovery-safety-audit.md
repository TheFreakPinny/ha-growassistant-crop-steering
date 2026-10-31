# Native Recovery Safety Audit — PR #108

## Auftrag, Basis und Ergebnis

Geprüft am 31.10.2026: `main` nach dem gemergten PR #107, Commit
`af15189047f4106a3da6e5b5e095109f0af02572`, Tree
`75ac683df59c82712353953c68c6ab6e66dd7e6b`.
Die [Coverage-Matrix](native-recovery-safety-coverage.md) enthält **35 stabile
Sicherheitsanforderungen**, konkrete Code-/Testanker, Voraussetzungen und Grenzen.
Der [Backlog](native-recovery-gap-backlog.md) enthält **8 P0, 4 P1, 2 P2, 1 P3**.

Ergebnis: umfangreiche fail-closed Softwarediagnostik und isolierte
Recovery-Negativnachweise sind vorhanden. **Produktive Native-Recovery-Sicherheit
ist nicht bewiesen.** Keine physische OFF-/Exklusivitätsgarantie, kein reales
Last-Point-Fencing, keine gemeinsame dauerhafte Multi-Store-Commit-Grenze und
keine sichere Recovery-Wiederaufnahme sind nachgewiesen. Kein reales
Betriebsevidenzpaket wurde im Repository oder in den zugehörigen PR-Nachweisen
gefunden. `PRODUCTION_VERIFIED` wird deshalb kein einziges Mal vergeben.

| Primäres Evidence-Level | Anforderungen |
| --- | ---: |
| CODE_ONLY | 3 |
| UNIT_TESTED | 4 |
| HARNESS_TESTED | 21 |
| INTEGRATION_TESTED | 2 |
| HARDWARE_UNVERIFIED | 5 |
| PRODUCTION_VERIFIED | 0 |

Diese Zählung bewertet Anforderungen innerhalb ihrer jeweiligen Grenzen.
Insbesondere sind CODE_ONLY-Gesamtansprüche und HARDWARE_UNVERIFIED offen;
auch ein HARNESS_TESTED-Teilanspruch schließt seine dokumentierte produktive
Lücke nicht. Die Testbaseline von 1.801 / 1.005 ist nur Umfangs-/Regressionskontext.

## Unveränderte Sicherheitsgrenze

Dieser PR verändert keine produktive Pythondatei und keinen produktiven Service.
Die geprüfte Recovery-Kette bleibt bei `native_activation_allowed=False`,
`coordination_ready=False`, `hardware_exclusivity_proven=False`.
Keine positive Cycle-Freigabe, kein Release-Permit, keine produktiven
Pumpenbefehle, keine Gate-Auflösung, kein Schedulerstart, keine Änderungen
historischer Accounting-Daten, keine Ersatzshots und kein automatisches Replay.

Diese Aussagen beziehen sich auf die Native-Recovery-Kette. Die Integration
enthält weiterhin bestehende Legacy-Services und `_CycleResetCoordinator`;
deren `async_start` ist kein Native-Schedulerstart. Native-Controllercode ist
teilweise für Unload und Diagnostik transitiv importiert. „Nicht aktiviert“
bedeutet **nicht**, dass jede Native-/Recovery-Klasse vom Produktionsimportgraphen
abwesend ist. Fake-Gateway, Transaktionsadapter und Crashharness liegen dagegen
ausschließlich unter `tests/native_harness`.

## Methode und Architektur-Inventur

Code gelesen: `pump.py`, `shot_engine.py`, `cycle.py`, `scheduler.py`,
`native_safety.py`, `native_diagnostics.py`, sämtliche `recovery_*.py`,
`actuator_contract.py` und die Fake-Gateway-/Recovery-/Transaction-/Crashmodule.
Konkrete Testkörper und ihre Fixtures wurden auf Assertions, Mocks, Zeitbasis,
Speichergrenzen und erreichbare Zustände geprüft; die Matrix nennt die jeweiligen
Testselektoren. Vorhandene Recovery-Design-, Release-, Gateway-, Revisions-,
Restart-/Crash- und End-to-End-Dokumente dienen als Kontext, nicht als Ersatz
für Implementierungs-/Testnachweise.

| Schicht | Implementierte Funktion | Tatsächlich nachgewiesene Grenze |
| --- | --- | --- |
| Pump / Shot / Safety | lokaler Owner, Intent-vor-ON, frische Software-OFF-Bestätigung, Deadline, Safety-Supervision, Unsicherheits-/Recoverylatches | HA-/Fake-Serviceverhalten; keine elektrische oder hydraulische Garantie (NRS-001–009). |
| Accounting / Cycle / Scheduler | konsumierte Operationsidentitäten, konservative Historie, Baseline-/Cycle-Blocker, lokales Quiesce, kein Retry nach Fehler | isolierte Softwarezustände; keine Recovery-Disposition oder Resume-Zulassung (NRS-010–012/034). |
| Snapshot / Ledger / Revision | immutable Beobachtungen, Work-/Unknown-Evidenz, Replayvalidierung, Hash-/Revisionsbindung, Completionprüfung | lokaler Runtime-/HA-Dateipfad; keine dauerhafte externe Trustwurzel (NRS-013–016/028). |
| Plan / Transaction / Release | read-only Findings, technische Prepare-/Commit-Historie, negative Releasequittungen | echter Planner bleibt blockiert; kein positiver Gate-Commit, kein Permit (NRS-017–020). |
| Participant / Writer | fünf Teilnehmer, vollständige Revisionsmenge, aktuelle Identität/Generation, Eventbeobachtung und Widerruf | best effort Beobachtung; Fremdwriter bleiben unbewiesen, Aggregation bleibt nicht bereit (NRS-002/020/030). |
| Actuator-Vertrag / Fake-Gateway | reine Wertvalidatoren, Fake-Leases, Queue-/Ausführungsfences, Deduplizierung, Prioritäts-OFF, Fake-Witness | Simulation ohne authentifizierten Transport oder realen Ausgang (NRS-021–023/029). |
| Fake-Recovery / Audit / Crashharness | gekoppelter Collector, Audit-Receipt/Witness, Barrier-Races, Rekonstruktion, historische Integritätsprüfung | kein Produktionsadapter, keine reale verteilte Transaktion (NRS-024–027/035). |
| Produktive Lifecyclegrenze | Setup bleibt diagnostisch/Legacy; Native-Initialisierung nicht verdrahtet | bestehende Lifecycletests plus zwei neue Regressionstests (NRS-031); kein allgemeiner Beweis gegen beliebigen dynamischen Pythoncode. |

Lock- und Serialisierungsgrenzen sind lokal: Recovery koordiniert vor Cycle und
Accounting; Stop/Widerruf soll nicht hinter Reconciliation warten. Prozesslokale
Ownerregistrierung und Busy-Flags können einem unabhängigen alten Prozess keine
physische Schreibfähigkeit entziehen. Ein Coroutine-Cancel beendet keinen bereits
laufenden Executorwrite oder zugestellten Gerätebefehl.

## Belegte Antworten auf die Architekturfragen

### Ownership und Writer-Isolation

**Nein, fremde Pumpenwriter können nicht ausgeschlossen werden.**
`PumpController` und `_ShotCoordinator` prüfen lokale Ownership und Zielbindung.
`_WriterObserver.capture` führt dauerhaft `writer_visibility_incomplete` und
`external_writers_unproven`; `exclusive_isolation_confirmed` ist konstant falsch.
`test_known_owner_and_silence_never_prove_exclusivity` sowie die
`missing_event`-Variante von `test_state_direct_access_or_monitoring_gap_is_fail_closed`
belegen die konservative Ablehnung selbst bei Ruhe. HA-Kontextfelder sind nur
Attributionshinweise. Geräteclients, Cloud, indirekte Targets und Geschichte vor
Monitoring sind nicht vollständig sichtbar. Voraussetzung: G01/G03, technische
Isolation aller realen Wege und authentische Hardwareidentität.

### Physisches OFF

**Kein unabhängiger Nachweis vorhanden.** `PumpController._stop_locked` verlangt
frischen HA-OFF-Bericht, abgeschlossene Servicearbeit und bestätigtes lokales
Löschen des Recoverytargets. Ein leerer Record kann beim Stop True ergeben,
ohne einen OFF-Beleg zu erzeugen; `test_idle_empty_journal_is_not_off_proof`
verhindert die Gleichsetzung. `test_off_receipt_is_software_only` und
`test_safety_receipts_never_attest_physical_off` begrenzen Receipts explizit.
Voraussetzung G02: unabhängige Kanal-/Strom-/Kontakt-/Flussevidenz nach letztem ON,
Drain/Tray und Nachlaufprüfung, qualifizierter unabhängiger Abschaltpfad.

### Fencing

**Bei Annahme und Ausführung im Fake implementiert; am physischen Endpunkt
unbewiesen.** `FakeGateway.accept/execute/_blocker` prüfen Lease, Epoche,
Sequenz, Deadline und Stopgeneration an modellierten Barrieren.
`test_revalidate_across_await` und `test_restart_orders_with_real_reconstruction`
prüfen veraltete Intents. Der ON-Fencing-Nachweis in letzterem Test ist an einen
Gatewayrestart gebunden. Ein isolierter HA-Restart informiert einen unabhängigen
Gateway nicht automatisch; eine noch gültige Lease wird nicht rückwirkend
widerrufen. Fake-`poll` ist kein real autonom laufender Hardwarewatchdog.
G03 muss auch Paketstau hinter dem Validator und Split Brain beherrschen.

### Persistenz und Rollback

**Lokale HA-Datei-/Executor-Completion ist geprüft; relevante Hardware- und
gemeinsame Rollbackfehler sind nicht abgesichert.** `_ParticipantStore` und
`JournalRevision` bestätigen nur tatsächlich abgeschlossene Writes; Timeout/
Cancellation behalten Ungewissheit auch bei späterem Erfolg.
`test_real_executor_late_completion_never_acknowledges_timed_out_write` prüft dies
am echten Executor. Hashketten erkennen inkonsistente Struktur, nicht authentische
Herkunft. `test_simultaneous_rollback_and_trust_limit` erkennt Rollback relativ zu
erhaltenem Witness und demonstriert die Grenze bei gemeinsamem Restore.
G04 benötigt eine unabhängige dauerhafte Wurzel und Power-Cut-Nachweise; MemoryStore-
Teilpersistenz modelliert keine Flashsektoren, fsync oder Controllercaches.

### Multi-Store-Konsistenz

**Koordinierte Best-Effort-Prüfung, keine nachgewiesene gemeinsame Commit-Grenze.**
`_RecoverySource.capture`, Planner und Transaktionskoordinator vergleichen
Beobachtungen/Revisionen vor und nach awaits. Das erkennt beobachteten Drift,
verhindert jedoch keine unsichtbaren externen Änderungen oder gemeinsamen
Rollback. `test_multi_store_write_errors`, `test_late_multi_store_confirmation`
und `test_combined_races` prüfen definierte Fehlerpräfixe. Audit-Reports lassen
`atomic_multi_store_commit=False`. Voraussetzung G05: teilnehmereigene Receipts,
konservatives globales Commit-/Reconciliationprotokoll und geschlossene Admission
bei jeder Teilpersistenz; kein automatisches Finalisieren unbekannter Outcomes.

### Recovery-Release

**Die heutigen Blocker sind real nicht nachweisbar auflösbar.**
`aggregate_evidence` ergänzt dauerhaft `ownership_epoch_unproven`,
`external_writers_unproven` und `coordinated_resolution_unimplemented`.
Der echte Plan wird vom technischen Prepare verweigert
(`test_real_protocol_stays_blocked`). `_CycleReleaseCoordinator.permit` liefert
immer None; `_receipt` ist immer negativ. Selbst ein modellierter vollständiger
technischer Commit lässt `participant_owned_gate_resolution_missing` bestehen.
Die Harness-Authority ist keine HA-Authentifizierung. Voraussetzungen G01–G07,
G09/G10; späterer separater Entscheidungsakt und Teilnehmerbarrier, kein Flag-Reset.

### Scheduler

**Lokales Stop/Drain und konservative Nicht-Wiederholung sind implementiert;
sichere Wiederaufnahme nach Recovery fehlt.** `_NativeScheduler.async_start`
ist eine explizite Harnessfunktion, kein Recovery-Admission-Protokoll.
`test_scheduler_stop_start_cannot_acknowledge_interruption` und
`test_scheduler_retry_latch_survives_diagnostic_check` belegen die erhaltene
Sperre. Normales Setup initialisiert keine Native-Owner. G08 verlangt getrennte
frische Admission nach vollständiger Gate-/Cycle-Disposition. Recovery oder
Gate-Auflösung darf keinen Start auslösen.

## Threat- und Failure-Model

Fehler werden als crash-stop, Verzögerung/Umordnung, veraltete/fehlerhafte
Beobachtung, Teilpersistenz oder Vertrauensverlust betrachtet. Gemeinsame
Fehlerdomänen werden nicht als unabhängig angenommen. Bösartige/kompromittierte
Clients sind im Sinne unautorisierter Writer berücksichtigt; eine vollständige
Cybersecurity-/Firmware-/Supply-Chain-Prüfung ist nicht geleistet.

| Szenario | Bestehende Schutzfunktion / konkrete Anforderung | Verbleibende Voraussetzung |
| --- | --- | --- |
| HA-Ausfall | Boot-/Runtimeinvalidierung, kein Replay, NRS-016/024 | autonomer OFF/Fence ohne HA, G03; echte Crashgrenzen G12 |
| Gateway-Ausfall | Fake-Bootwechsel, alte Tokens ungültig, NRS-022/024 | realer Boot-OFF und Split-Brain-Enforcement G02/G03 |
| Netzwerkverlust | Softwaretimeouts, Ungewissheit; Fake connection_lost/Lease, NRS-005/023 | lokale Hardwaregrenze, Queue-Drain, G03 |
| Relais bleibt eingeschaltet | kein physischer Schutz nachgewiesen, NRS-003 | unabhängiger Abschalt-/Feedbackpfad G02 |
| Pumpenaktor reagiert nicht | fehlendes/falsches Ack blockiert Software, NRS-005 | tatsächlichen Ausgang stoppen/verifizieren G02/G03 |
| Verspäteter ON-Befehl | Late-Service-Reconciliation, Fake-Last-Apply-Fence, NRS-005/022/029 | letzter physischer Enforcer G03; späteres OFF allein verhindert vorigen Wasserfluss nicht |
| Fremder Writer | Eventbeobachtung widerruft Diagnose; fehlendes Event bleibt unbewiesen, NRS-002/030 | Pfade technisch ausschließen G01 |
| Fehlerhafte Sensorwerte | Plausibilität, Spread, Freshness, NRS-008 | glaubwürdig falsche/replayed Werte und Kalibrierung G11 |
| Pumpenstatus unbekannt | Unknown-Evidenz / Gateblocker, NRS-003/005/013 | unabhängiges aktuelles Feedback G02 |
| Journalverlust | strikte Loader, fehlende Revisions-/Witnessbelege blockieren, NRS-014/015/025 | Verlust aller Wurzeln nicht erkennbar; G04 |
| Teilpersistenz | Unsicherheitslatch und Konsistenzverweigerung, NRS-015/027 | keine gemeinsame Atomizität; G04/G05 |
| Storage-Rollback | erhaltene Revision-/Witnessreferenz erkennt Konflikt, NRS-025/028 | unabhängige nicht mit restaurierte Wurzel G04 |
| Gleichzeitiger Rollback mehrerer Stores | Test demonstriert Grenze bei Witness-Rollback, NRS-025/027 | keine pauschale Erkennung; G04/G05 |
| Eigentümerwechsel | lokale Ownership-/Safetygeneration, Fake-Leasewechsel, NRS-001/016/022 | echte alte Writer am Aktor ausschließen G01/G03 |
| Veraltete Lease | reiner Validator und Fake-Deadline-/Epochenprüfung, NRS-021/022 | authentischer Enforcer/monotone Uhr G03 |
| Wiederanlauf mit ungeklärtem Shot | konsumierte Identität, Partial Delivery bleibt gesperrt, NRS-009/024 | G06/G10; keine Ersatzshots oder automatische Wiederholung |
| Unbeabsichtigter Schedulerstart | inaktives Setup, Retrylatch, negative Lifecycleguards, NRS-012/031/034 | sichere gesonderte Admission fehlt, G07/G08 |

## Fehlende Negativnachweise und Beobachtbarkeit

Die wichtigsten offenen Negativtests brauchen eine andere Testumgebung: reale
Bypasszugriffe, klebendes Relais bei gleichzeitig falschem OFF-Bericht,
Gateway-/HA-Netzpartition ohne gemeinsamen Restart, alter Controller im zweiten
Prozess, echte Power-Cuts und gleichzeitig restaurierte Daten/Witnesses.
Kein künstlich positives Fake-Review kann diese Lücken schließen.

Weitere offene Racegrenzen: parallele Threads/Prozesse, Gerätequeue hinter dem
letzten Softwarecheck, Torn Writes im tatsächlichen Storage und uneingeschränkte
Event-Loop-Last. Der synchrone Fake-Gatewaykonstruktor hat keine interne
await-Barriere; ein Crash zwischen beliebigen Anweisungen wird nicht simuliert.
Der Watchdogtest `test_monotonic_watchdog_stops_without_a_caller_stop` nutzt
eine reale Loopdeadline von 0,05 s gegen FakePump, prüft aber nur einen
**Mindestabstand** von 0,045 s zwischen ON und OFF, keine obere Stoplatenz.
Er beweist deshalb keine maximale Abschaltzeit (NRS-032 / G13).
Ein leerer Protokollstore und ein fehlgeschlagener externer Write vor jeder
Änderung können diagnostisch konsistent bleiben. Integritätsprüfung ist keine
vollständige Fehlertelemetrie. HA-Eventhistorie ist auf 128 Beobachtungen begrenzt;
Überlauf bleibt sticky visibility loss, nicht stillschweigend positive Ruhe.

Positive Modellfälle und künstlich eligible Protokollpläne sind in der
[Coverage-Matrix](native-recovery-safety-coverage.md) einzeln eingegrenzt.
Kein Test erzeugt einen unabhängigen Realnachweis durch seinen Namen oder Umfang.

## No-Go-Kriterien und nächste Schritte

Keine produktive Aktivierung bei einem offenen P0, unbekanntem Pumpenzustand,
fehlendem unabhängigen OFF, nicht technisch exklusiver Ownership, Fencing nur
vor dem Transport, ungeklärter Teilförderung, fehlenden/ungewissen Revisionen,
gemeinsam restaurierbarer Trustwurzel, partiellen Teilnehmercommits oder einer
Schedulerzulassung aus bloßer Diagnose/Review/Commit-Historie.

Reihenfolge: **Enforcement und physische Evidenz → dauerhafte Trustwurzel und
Multi-Store-Strategie → Operations-/Accounting-/Cycle-Disposition → authentische
Teilnehmerverträge und gemeinsamer Barrier → separate Scheduler-Admission →
eigenständiger Installations-/Aktivierungsreview**. Details und Abschlussnachweise
stehen im Backlog. Dieser PR ist keine Freigabe für einen dieser Aktivierungsschritte.

## Validierung dieses PRs

Python 3.13.16, Home Assistant 2025.12.5, HA-Fixture 0.13.301,
pytest 9.0.0 und Ruff 0.17.0 gemäß den bestehenden CI-Pins. Zwei zusätzliche
Tests: NRS-031, keine neuen Produktivdateien und keine Friständerung.

| Prüfung | Ergebnis |
| --- | --- |
| Gesamte Native-Harness-Suite (`pytest -q tests/native_harness --tb=short`) | **1.007 bestanden**, 162,46 s, einschließlich beider neuer Guards |
| Vollständige pytest-Suite | Läuft; Ergebnis wird vor Abschluss ergänzt |
| Ruff (`ruff check custom_components tests scripts`) | Bestanden |
| Format (`ruff format --check custom_components tests scripts`) | Bestanden, 75 Dateien |
| Kompilierung (`compileall -q custom_components tests scripts`) | Bestanden |
| Git-Whitespace | Wird am finalen Diff erneut geprüft |
| HACS / Hassfest | Werden über die unveränderten PR-Workflows ausgeführt; Links folgen |

Alle pytest-Läufe laufen nacheinander, nicht unter konkurrierender Testlast.
Die anfängliche Umgebung enthielt die HA-Testfixture/Supporting Dependencies
nicht; die gepinnten CI-Abhängigkeiten und Fixture wurden nachinstalliert.
Ein erster Sandboxlauf blieb in der HA-Fixture hängen und wurde nach einem
Timeout abgebrochen. Die erfolgreiche Harnessvalidierung läuft mit den für die
isolierte HA-Fixture nötigen Ausführungsberechtigungen. Ein erster neuer Guard
patchte irrtümlich eine schreibgeschützte ServiceRegistry-Instanzmethode; er
wurde auf den Klassenpatch umgestellt und bestand in der kompletten Suite.
Keine dieser Einrichtungs-/Testkorrekturen verändert produktive Sicherheitslogik.

35 primäre Testanker und die Links wurden zusätzlich auf Existenz geprüft.
Die Dokumente beschreiben keine automatisch gemessene Codecoverage und keine
allgemeine Sicherheitsfreigabe.
