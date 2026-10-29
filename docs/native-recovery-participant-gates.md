# Teilnehmereigene Recovery-Gate-Koordination

Basis: `main` nach PR #97, `e50e0a128859e415a7d0f5d2f109d87444a1f6d2`.
Dieser Baustein konkretisiert die Teilnehmergrenze aus
[native-recovery-release-design.md](native-recovery-release-design.md),
[native-recovery-release-implementation.md](native-recovery-release-implementation.md),
[native-recovery-transactions.md](native-recovery-transactions.md) und
[native-recovery-plans.md](native-recovery-plans.md).

## Ergebnis und ausdrücklich verbleibende Lücke

`recovery_participant.py` implementiert einen internen, lesenden Vertrag und
`_RecoveryGateCoordinator.async_check()` zur gemeinsamen Diagnose. PumpController,
ShotCoordinator, CycleController und Scheduler erzeugen ihre eigenen typisierten
Nachweise über `recovery_gate_evidence(request)`. Accounting gehört dem
ShotCoordinator; es erhält einen separaten Nachweistyp. Der Coordinator setzt
keine privaten Teilnehmerflags und ruft keine mutierende Bereitschaftsprüfung
wie `cycle.permits()` auf.

**Die aktuelle Architektur erlaubt weiterhin keine vollständig positive
Koordinationsbereitschaft.** `collection_complete=True` bedeutet ausschließlich,
dass alle fünf Nachweistypen erfasst wurden. Es ist auch bei negativen Ergebnissen
möglich. `coordination_ready` bleibt `False`, bis dauerhafte Teilnehmerrevisionen,
Ownership-/Writer-Isolation und koordinierte Auflösung separat implementiert
und geprüft sind. `confirmation_current(result)` bestätigt nur die Identität und
aktuelle lokale Bindung dieses diagnostischen Ergebnisses, einschließlich seiner
negativen Befunde. Es bestätigt weder die Erfüllung aller Voraussetzungen noch
dauerhafte Aktualität aller Dateien. Es ist kein Release-Permit.

Ein „erfolgreicher diagnostischer Test“ prüft daher die vollständige Sammlung
realer Teilnehmernachweise und ihre unveränderten Sperren. Es gibt keinen durch
Ersetzen von Findings, privaten Flags oder Journalen erzeugten grünen
Recovery-Pfad. Bestehende Plan-, Transaktions- und Release-Policies bleiben
unverändert; sie konsumieren diese neuen Nachweise noch nicht als Autorität.

Interner Harness-Aufruf (keine Einbindung in Integration-Setup oder HA-Service):

```python
coordinator = _RecoveryGateCoordinator(ledger, scheduler)
try:
    result = await coordinator.async_check()
    # collection_complete: fünf erfasste Typen, auch wenn fachlich negativ.
    # confirmation_current: lokale Aktualität dieser konkreten Diagnose.
    # blockers: vollständige Vereinigung; coordination_ready bleibt False.
finally:
    coordinator.close()  # Listener entfernen und Diagnose widerrufen.
```

Die Reservation bleibt nach `close()` für diese HA-Laufzeit erhalten. Ein neuer
Versuch erfolgt über dieselbe offene Instanz; es gibt keinen Reset der
Teilnehmergates und keine automatische Fortsetzung nach einem Neustart.

## Vier getrennte Aussagen

| Aussage | Darstellung | Bedeutung |
| --- | --- | --- |
| Historischer Sicherheitsfehler | `historical_errors`, ursprüngliches Operationsjournal und Ledger | Fehler bleibt sichtbar, auch nach einem Review |
| Offener Recovery-Blocker | `blockers`, fehlgeschlagene oder unbekannte `GateCheck`s | Nicht durch einen anderen erfolgreichen Teilnehmer aufhebbar |
| Geprüfte fachliche Disposition | `OperationDisposition.reviewed/checked/disposition` | Zum Beispiel geprüftes `retain_locked`; keine nachträgliche Behauptung voller oder ausgebliebener Lieferung |
| Laufzeitgebundene Bestätigung | exakter ausgegebener `GateRequest`, Teilnehmer-Epoche, `confirmation_current` | Teilnehmer bestätigt seine aktuellen Beobachtungen; eine negative Bestätigung bleibt negativ |

Die Shot-Prüfung überprüft die Bindung der Operationsdaten und den aus dem
validierten Ledger-Replay abgeleiteten Review. Ein geprüftes `retain_locked`
kann `checked=True` haben, während `partial_delivery_unresolved` weiter blockiert.
Die historische Operation und ihre Fehlermeldung werden dabei weder geändert
noch gelöscht. Ein fehlender Review bleibt `acknowledgment_missing`. Die
bisherigen strengeren Snapshot-Findings bleiben zusätzlich erhalten.

## Unveränderliche Datenverträge

Alle Datensätze sind eingefrorene Dataclasses. Ausgegebene Sammlungen bestehen
aus Tupeln und weiteren eingefrorenen Datensätzen; es werden keine veränderlichen
Journalreferenzen ausgegeben. Der Vertrag hat keine Persistenzdarstellung und
keinen Loader für laufzeitgültige Bestätigungen.

`GateRequest` bindet einen neu erzeugten Versuch an:

- exaktes `ResourceBinding`: Entry, Pumpenentity sowie Registry-/Gerätebindung,
  soweit bekannt; unbekannte physische Identität bleibt ein Blocker;
- den aktuellen Cycle und den vollständigen beobachteten Operationsumfang,
  einschließlich der eigenen historischen Cycle-ID jeder Operation;
- Digest der unveränderten Operationsdaten, geprüften Review-Status und
  diagnostische Disposition;
- `EvidenceRevision` für Pumpe, Shots, Accounting, Cycle, Scheduler und Ledger;
- frische Laufzeitepoche, Sicherheitsgeneration und Erfassungszeit.

Die Revisionen enthalten Inhaltsdigests und die tatsächlich verfügbaren dauerhaften
Revisionsnummern. Die Ledgerrevision ist vorhanden; die vorhandenen Pumpen-,
Shot- und Cycle-Stores besitzen **keine** monotone dauerhafte Revision. Diese
bleibt `None`. Digests sind Vergleichswerte, keine Ersatzrevisionen, Signaturen,
Ownership-Berechtigungen oder Schutz gegen Rollback/externe privilegierte Writer.
Accounting bindet an dieselbe Shotquelle, bleibt aber ein eigener Nachweis.

Jede Komponente hat eine neue flüchtige Instanz-Epoche. Ein HA-Neustart konstruiert
neue Komponenten und einen neuen Collector; gespeicherte Historie kann keine
Bestätigung wiederherstellen. Der Collector reserviert das ResourceBinding für
die HA-Laufzeit. Parallele Prüfungen werden mit `participant_check_busy`
abgewiesen, weitere Collector-Instanzen mit `writer_exists`.

`request_current` vergleicht die exakte Objektidentität des ausgegebenen Requests,
Laufzeitepoche, Generation und eine maximale diagnostische Lebensdauer von 30 s.
Eine gleichwertige Kopie kann keinen geprüften Review vortäuschen. Diese Grenze
ist ein interner Prozessvertrag, keine öffentliche HA-Authentifizierung.
Privilegierter Code im selben Prozess bleibt vertrauenswürdig; die Schnittstelle
darf nicht an einen öffentlichen Service angeschlossen werden.

## Verantwortlichkeiten und Prüfungen

| Eigentümer / Typ | Eigene Prüfung | Verbleibende Grenze |
| --- | --- | --- |
| PumpController / `PumpGateEvidence` | Eigene Registrierung, Ziel, Recovery-Record, aktuelle OFF-Zeit und Reihenfolge, Service-/Stop-/Safety-/Storage-Arbeit und Ownership-Aussage | Kein unabhängiger physischer OFF-Beleg, keine dauerhafte Command-/Ownership-Epoche |
| ShotCoordinator / `ShotGateEvidence` | Vollständige Operationsmenge, unveränderte Inhalte, Cycle-Zuordnung, verbrauchte Identitäten, Review und konservative Disposition, Task-/Recoveryzustand | Kein Modell zur Auflösung unklarer Teilbewässerung; keine Journalreparatur |
| ShotCoordinator / `AccountingGateEvidence` | Lokale Zähler, Accounting-Intent, Checkpoint und Operationskonsistenz | Keine atomare, revisionierte Vereinbarung zwischen Optionen, Shotjournal und Cycle-Store |
| CycleController / `CycleGateEvidence` | Cycle, Phase, Flags, Schedule, P2-Referenz, Pending/Storage, eigene Zuordnung | Keine geprüfte neue Baseline; `baseline_established=False` |
| Scheduler / `SchedulerGateEvidence` | Eigene Registrierung, Task, Timer, vorgemerkte Auswertung, Generation und Retry-Latch | Lokale Ruhe ist keine dauerhaft vereinbarte Admission-/Quiesce-Epoche |

Der Scheduler wird durch die Prüfung weder gestartet noch gestoppt. Die Diagnose
beobachtet Arbeit und lehnt sie ab, statt sie selbst zu drainen. Der Retry-Latch
bleibt unverändert. Ein erfolgreicher `scheduler_idle`-Check kann weder einen
Pumpenfehler noch einen Accounting- oder Cycle-Blocker verdecken.

## Unterbrochene Shots und spätere Cycles

Die historische Operations-ID bleibt dauerhaft im unveränderten Shotjournal
verbraucht. `operation_consumed=True` beschreibt diese Eigenschaft. Ein erneuter
Aufruf derselben ID scheitert weiterhin an `duplicate_operation`.

Eine mögliche Teilbewässerung wird weder zu `software_completed` noch zu
`not_requested` umgeschrieben. `volume` bleibt `None`. Ungeklärte Auswirkungen
blockieren denselben Cycle. Weder ein Review noch eine neue kalendarische
Cycle-Grenze beweisen eine neue sichere Baseline.

`CycleGateEvidence.required_new_cycle_checks` benennt die später erforderlichen
fachlichen Nachweise: agronomische Auswirkungen, Substrat-/Drain-/Trayzustand,
Wasserbudget-Disposition, begründete Warte-/Beobachtungsentscheidung, ausdrücklich
geprüfte neue Cycle-Baseline, Accounting-Provenienz sowie frische OFF-, Ownership-
und Sensornachweise. Es gibt hierfür noch keine strukturierte persistente
Nachweis- und Freigabeimplementierung. Die Liste führt keine dieser Handlungen
aus und ist kein Formular zum Übergehen der Sperre. Keine Ersatzbewässerung,
Volumenschätzung, automatische Baseline oder Zählerkorrektur wird eingeführt.

## Aggregation, Reason-Codes und Konkurrenz

Die Aggregation bildet die Vereinigung aller Blocker, niemals den Status des
zuletzt erfolgreichen Teilnehmers. Sie verlangt genau einen Nachweis jedes
konkreten Typs. Fehlende, doppelte, unbekannte oder anders gebundene Nachweise
bleiben negativ. Alle bisherigen Plan-Findings und offenen Prüfungen werden
zusätzlich erhalten; kein alter Sicherheitsbefund wird ausgefiltert.

| Code | Bedeutung |
| --- | --- |
| `participant_missing`, `participant_conflict`, `participant_invalid` | Fehlender, mehrfacher oder unbekannter Nachweistyp |
| `confirmation_binding_conflict`, `confirmation_invalid`, `confirmation_not_current` | Andere Versuchs-/Cycle-/Operations-/Revisions-/Laufzeitbindung, fehlende Identität oder nicht exakt ausgegebener aktueller Request |
| `binding_mismatch`, `cycle_mismatch` | Abweichendes Ziel bzw. aktueller Cycle |
| `operation_scope_conflict`, `operation_cycle_conflict`, `operation_evidence_changed` | Unvollständige Operationsmenge oder widersprüchliche Operationsbindung |
| `revision_conflict`, `durable_revision` | Veralteter/fehlender Inhaltsbeleg bzw. fehlende dauerhafte Revision |
| `ownership_lost` | Teilnehmer besitzt seine lokale Registrierung nicht mehr |
| `scheduler_work_unresolved`, `scheduler_admission_epoch_unproven` | Laufende/vorgemerkte Schedulerarbeit bzw. fehlender Quiesce-Vertrag |
| `acknowledgment_missing`, `partial_delivery_unresolved`, `cycle_disposition_unproven` | Offener Review, unklare Lieferung oder ungeprüfte fachliche Cycle-Disposition |
| `evidence_changed`, `participant_evidence_invalid` | Änderung während der Prüfung oder fehlgeschlagene Evidenzerfassung |
| `durable_participant_revisions_unproven`, `ownership_epoch_unproven`, `external_writers_unproven`, `coordinated_resolution_unimplemented` | Unbedingte aktuelle Architekturgrenzen; keine Überschreiboption |

Snapshot- und Plan-Codes behalten ihre bisherige Bedeutung, insbesondere
`command_unresolved`, `task_unresolved`, `storage_unresolved`,
`accounting_inconsistent`, `cycle_inconsistent`, `ownership_conflict` und
`legacy_conflict`. Unbekannte/fehlende Nachweise werden nicht als Erfolg behandelt.

Vor und nach der Teilnehmererfassung läuft der bestehende lesende Planer mit
seinen dauerhaften Store-/Ledgervergleichen. Ein zusätzlicher synchroner Guard
bindet Teilnehmerzustände, Ledger, Konfiguration, Sensorpolicy, direkte Sensor-/Drain-/
Trayzustände samt Reportzeiten, Sensorassessment, Ownership und Generation. Nach dem letzten `await` werden alle Teilnehmer erneut synchron
befragt; es gibt kein `await` vor der Veröffentlichung des diagnostischen
Ergebnisses. Änderungen machen die gesamte aktuelle Bestätigung ungültig.

State-Events, Entry-Updates, HA-Stop, Core-Konfiguration und explizites `revoke()`
invalidieren die Diagnosegeneration. State-Events werden konservativ auch für
andere Entities erfasst; auch ein beobachtetes ON/OFF-ABA invalidiert. Neuere
Versuche ersetzen die Identität älterer Ergebnisse. Cancellation veröffentlicht
keine Bestätigung und propagiert; Lesefehler bleiben negative Diagnosen.

Dies ist **keine** globale Serialisierung der Teilnehmer oder externer Writer.
Direkte Änderungen außerhalb beobachteter Events können bei einer Rückkehr zum
identischen Zustand unentdeckt bleiben. `confirmation_current` prüft lokale
Zustände synchron und führt keine neuen Dateileseoperationen durch. Spätere
Disk-only-Änderungen erfordern erneut `async_check()`. Es gibt keine Garantie
gegen einen alten HA-Prozess mit laufender Executor-IO. Diese Grenzen verhindern
bereits die positive Koordinationsbereitschaft; sie werden nicht als gelöst
behauptet.

## Persistenz und später erforderliche Gate-Auflösung

Dieser PR schreibt keinen neuen Store und keine Teilnehmerquittierung auf Disk.
Daher gibt es keinen neuen Audit-Write, der durch verspätete IO ein Gate öffnen
könnte. Bestehende Pending-, Failed- und unsichere Writes bleiben Blocker. Eine
verspätete erfolgreiche Teilnehmerdatei heilt keinen Recovery-Latch. Lesefehler
und beschädigte oder abweichende Evidenz erzeugen keine Bestätigung des Erfolgs.

Eine spätere Auflösung benötigt separat geprüfte dauerhafte Teilnehmerbelege,
serialisierte Admission-/Command- und Ownership-Generationen, effektive
Writer-Isolation, fachliche Dispositionen und Accounting-/Baseline-Provenienz.
Danach sind ein koordinierter Intent, erneute Teilnehmerprüfung, crashsicherer
finaler Commit, eine separate ausdrückliche Freigabeentscheidung und eine finale
synchrone Live-Prüfung erforderlich. Erst ein solcher eigener Vertrag dürfte
jede Komponente zur Auflösung **ihres eigenen** Gates veranlassen. Ein zentraler
Collector dürfte weiterhin keine privaten Latches löschen. Crashs an jeder
Durable-Prefix-Grenze müssen gesperrt booten; gespeicherte Historie darf nie ein
laufzeitgültiges Permit rekonstruieren.

Dieser PR führt keine Gate-Mutatoren, Release-Permits, öffentliche Freigabe-API,
Dashboard-Schaltfläche, Pumpensteuerung, Scheduler-Aktivierung, Shot-Replays oder
Ersatzshots ein. Bestehende Zähler, Operationshistorien und produktives
Integrationsverhalten bleiben unverändert. `native_activation_allowed` bleibt
in allen Datensätzen und Ergebnissen unveränderlich `False`.

## Validierung

Neue Fake-Harness-Tests verwenden tatsächliche unterbrochene Shots, Ledgerreviews
und Lifecycle-APIs. IO-Barrieren halten tatsächlich vom Teilnehmer erzeugte
Service-/Storage-/Schedulerarbeit an; private Gates und Journale werden nicht
für positive Ergebnisse überschrieben. Reale HA-Service-Dispatches sind durch
die bestehende Fixture verboten. Vollständige Checkpoints, Commands, Ledger,
Zähler, Duplikatsperre und Recovery-Gates werden unabhängig kontrolliert.

Umgebung: Python 3.13.16, Home Assistant 2025.12.5, pytest 9.0.0,
Ruff 0.17.0. Interpreter: `/workspace/growassistant-cloud/venv/bin/python`.
Pytest läuft sequenziell mit `LITELLM_LOCAL_MODEL_COST_MAP=True` und der
unterstützten Netzwerkberechtigung für den Command. Die fehlende HA-Fixture und
CI-Abhängigkeiten wurden entsprechend der bestehenden CI-Konfiguration
(`.github/requirements-ci.txt` und `--no-deps` für die Fixture 0.13.301) in der vorhandenen isolierten Venv installiert; keine Dependencydatei
wurde geändert. Die bekannte Fixture-Metadatenabweichung zu HA 2025.12.4 bleibt
wie in den vorherigen Phasen bestehen; getestet wird der Repository-Pin 2025.12.5.
Der Git-Fetch benötigte ebenfalls die unterstützte Command-Netzwerkberechtigung.
Der Checkout basiert vollständig auf dem echten `origin/main`, ohne
Dateirekonstruktion aus Connector-Antworten.

| Prüfung | Ergebnis |
| --- | --- |
| Neue Teilnehmer-Gate-Tests | **52 bestanden**, 2,89 s |
| Native-Harness-Suite | **411 bestanden**, 66,84 s |
| Vollständige pytest-Suite | **1.207 bestanden**, 119,03 s; keine Fehler/Skips |
| `ruff check custom_components tests scripts` | Bestanden |
| `ruff format --check custom_components tests scripts` | Bestanden, 59 Dateien |
| `compileall -q custom_components tests scripts` | Bestanden |
| `git diff --cached --check` | Bestanden, einschließlich aller neuen Dateien |

Die Endsumme ist **1.155 Baseline + 52 neue Tests = 1.207**. Bestehende Tests und
Assertions wurden nicht verändert. Alle finalen Suiten liefen sequenziell ohne
konkurrierenden pytest-Prozess. Der abschließende Fetch bestätigte unverändert
`origin/main` bei `e50e0a1` (PR #97).
