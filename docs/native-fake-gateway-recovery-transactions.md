# Isolierte Recovery-Transaktionskopplung mit Fake-Gateway (PR #105)

Die Implementierung liegt ausschließlich in
`tests/native_harness/fake_gateway_transactions.py`. Produktive Einstiegspunkte,
Sicherheitsflags, Teilnehmer-Gates, Scheduler und Accounting bleiben unverändert.

## Gemeinsame Prüfungsidentität

Der eingefrorene `TransactionContext` bindet eine UUID für Transaktion und
Adapterinstanz sowie die Request-ID an Entry/Pumpenressource, Gateway-Boot und
Runtime, Ownership-Epoche, Stop-Generation, Journalrevision und Witness-Status.
Er enthält außerdem Ledgerrevision, Plan-ID und den bestehenden `plan_digest`,
Teilnehmerrevisionen, Cycle-ID, Operationsumfang, vollständige Teilnehmeranfrage,
Gateway-Snapshot, Recovery-Scope und den Stand des technischen Protokolls.
Die vollständigen Nachweise werden als kanonische JSON-Zeichenketten eingefroren;
keine veränderlichen Journalcontainer gelangen in den Kontext.

Die Gateway- und HA-Ownership-/Safety-Generationen bleiben getrennte Namensräume.
Die vollständigen HA-Runtime-Epochen sind im Recovery-Scope enthalten. Eine
Modellbindung beweist keine physische Pumpenidentität oder Exklusivität.
Fehlende oder widersprüchliche Identitäten und Revisionen bleiben blockierend.

## Prepare und technische Commit-Prüfung

`async_prepare` verlangt explizit erwartete Plan-ID, Plan-Digest und
Ledgerrevision. Es sammelt die tatsächlichen Teilnehmer- und Gateway-Nachweise
über PR #104 und erzeugt den Reconciliation-Plan erneut. Der Vergleich verwendet
`_observations` aus dem bestehenden Transaktionsprotokoll: Nur die pro Aufnahme
neuen Plan-ID/Zeitstempel werden beim Vergleich der *frischen* Beobachtung
ausgenommen. Der übergebene Plan selbst bleibt mit ID und vollständigem Digest
gebunden. Findings oder offene Checks werden niemals überschrieben.

Nach sämtlichen await-Grenzen werden Plan, Ledger, Protokoll und aktuelle
Gateway-/Teilnehmerbestätigung erneut geprüft. Offene/ungewisse Commands,
fehlende OFF-Evidenz, Witness-Konflikte, veränderte Ownership, Safety-OFF,
Boot-/Runtime-Wechsel oder Revisionen verhindern positive Konsistenzdiagnostik.
Ungeklärte Teilbewässerung bleibt selbst bei widerspruchsfreien Nachweisen ein
Recovery-Blocker. Software-OFF des Fake ist kein physischer OFF-Nachweis.

`async_commit` akzeptiert ausschließlich das unveränderte, identische
Prepare-Ergebnis derselben lebenden Adapterinstanz. Kopien, alte Ergebnisse,
Neustarts und Wiederholungen sind ungültig. Jeder Commitversuch verbraucht den
Versuch auch bei Abbruch oder Fehler. Er prüft den gebundenen Plan, alle aktuellen
Teilnehmer-/Gateway-Generationen und das technische Protokoll erneut, liest das
Audit-Journal, schreibt einen rein historischen Receipt und liest ihn zurück.
Nach der Schreibbestätigung folgt eine weitere vollständige Revalidierung.

| Aussage | Bedeutung |
| --- | --- |
| `evidence_consistent` | Die gebundenen Nachweise sind zum letzten Prüfzeitpunkt konsistent |
| `audit_persisted` | Der Audit-Receipt wurde nach bestätigtem Schreiben zurückgelesen |
| `prepare_blocked` | Immer `True`: kein tatsächlich vorbereiteter Recovery-Übergang |
| `recovery_release_allowed` / `cycle_release_allowed` | Immer `False` |
| `native_activation_allowed` / `coordination_ready` / `hardware_exclusivity_proven` | Immer `False` |
| `release_permit` | Immer `None` |

Ein Receipt kann gespeichert sein, während die abschließende Konsistenzprüfung
bereits fehlschlägt. Sein Feld `evidence_consistent_at_check` beschreibt nur die
Beobachtung **vor** der Schreiboperation. Es bescheinigt keine atomare
Gateway-/Recovery-Transaktion und keine heutige Gültigkeit.

## Wiederverwendung und Architekturgrenze

Die Kopplung verwendet `PROTOCOL`, `plan_digest`, `_observations` und den echten
`_RecoveryTransactionCoordinator` einschließlich seines unveränderten
Protokolljournals. Dessen gespeicherter Stand wird gegen seinen tatsächlichen
In-Memory-Stand geprüft; gesperrte, nicht initialisierte oder beschäftigte
Koordinatoren verhindern positive Konsistenzdiagnostik.

Das vorhandene Protokoll verlangt `preparation_eligible`, leere Findings und
leere offene Checks. Diese Voraussetzungen sind mit den echten Recovery-
Teilnehmern aktuell nicht erfüllt. Deshalb ruft der Adapter weder dessen
`async_prepare` noch `async_commit` auf. Er protokolliert eine Korrelation eines
**blockierten** Prepare, ohne konkurrierende Recovery-Zustände einzuführen.
Es werden keine synthetisch positiven Pläne oder privaten Gate-Flags benötigt.

## Audit-Persistenz und Crash-Verhalten

Das zusätzliche Audit-Journal verwendet die asynchrone `async_load`/`async_save`-
Schnittstelle. Im isolierten Harness wird es mit `MemoryStore` und getrenntem
`AuditWitness` über simulierte Neustarts erhalten. Dies ist ein deterministisches
Persistenzmodell, keine zugesagte Dateisystem-/fsync-Garantie. Receipts enthalten
Kontext, Blocker, Vorgängerhash und Hash. Der separat erhaltene Witness bindet
das erwartete vollständige Journal. Schema, Hashkette und Witness werden beim
Laden geprüft; Rollback, fehlender Witness oder beschädigte Daten sperren.

| Unterbrechung | Verhalten |
| --- | --- |
| Vor Prepare | Keine aktive Sitzung, keine Freigabeberechtigung |
| Während Prepare | Kein veröffentlichtes Ergebnis; Abbruch veröffentlicht keine Sitzung |
| Nach Prepare / vor Commit | Nur prozesslokale Referenz; nach Neustart verloren |
| Während Commit vor Schreiben | Kein Receipt und keine Freigabeberechtigung |
| Schreiben scheitert vor Persistenz | Versuch verbraucht; kein Retry |
| Schreiben persistiert und scheitert danach | Ungewisser Zustand; kein bestätigter Receipt, Witness bleibt alt |
| Bestätigung verspätet oder nach Abbruch | Kein Wiederaufleben der Sitzung; spätes Journal ohne passenden Witness wird abgelehnt |
| Nach Persistenz, vor Witness-Aktualisierung | Neustart erkennt Witness-Konflikt und sperrt |
| Nach Commit | Receipt ist ausschließlich historische Evidenz; Neustart rekonstruiert keinen Prepare |
| Gateway-Neustart | Boot-/Runtime-/Lease-Nachweise veraltet |
| HA-Neustart | Teilnehmer-/Owner-Epochen und Adapteridentität veraltet |
| Journal-Rollback | Abweichung zum separat erhaltenen Witness sperrt |

## Multi-Store-Grenzen und Races

Es gibt keine Multi-Store-Atomizität zwischen Gateway, Ledger,
Teilnehmerjournals, technischem Protokoll und Audit-Journal. Der Adapter schreibt
nur das Audit-Journal. Auch Audit-Journal und Witness bilden keine atomare
Speichereinheit. Ein gemeinsamer Rollback aller Vertrauenszustände ist mit diesem
Modell nicht erkennbar; ein Hash ist keine externe Authentifizierung.

Deterministische Barrieren `prepare_captured`, `commit_checked` und
`commit_saved` erlauben Race-/Crash-Injektion. Die bestehenden PR-#104-Barrieren
bleiben unverändert. Nach der letzten asynchronen Prüfung wird das Ergebnis
synchron veröffentlicht. Eine pro Ressource geteilte Harness-Serialisierung
weist überlappende Adapteroperationen ab. Sie ist keine Hardware-Exklusivität
und keine Garantie gegen externe Prozesse/Threads. Andere Diagnosen können die
transiente Teilnehmerbestätigung ersetzen und machen alte Versuche ungültig.
Kein automatischer Retry, Replay, Ersatzshot oder Schedulerstart findet statt.

## Verbleibende Blocker und spätere Gate-Auflösung

Alle ursprünglichen Gateway-, Teilnehmer- und Planblocker bleiben erhalten,
zusätzlich `recovery_prepare_blocked`. Insbesondere bleiben
`partial_delivery_unresolved`, `cycle_disposition_unproven`,
`external_writers_unproven`, `ownership_epoch_unproven` und
`coordinated_resolution_unimplemented` blockierend.

Eine echte spätere Freigabe benötigt belegbare physische Ressourcenidentität,
physisches OFF, Exklusivität gegenüber allen Writern, rollbackfeste
Vertrauensbasis, Commands mit Cycle-/Operationsprovenienz, geklärte Fördermenge
und Disposition, generationsgebundene Teilnehmer-Commit-/Fencing-Verträge und
einen geprüften Cycle-/Accounting-/Baseline-Übergang. Das bestehende technische
Protokoll muss diese tatsächlichen Nachweise dann selbst akzeptieren können.
Ein Fake-Receipt ersetzt keinen dieser Nachweise.

## Validierung PR #105

Baseline: 1.537 Tests, davon 741 Native-Harness. **66 zusätzliche Tests** mit
unveränderten echten Plan-Findings und deterministischen Barrieren. Python
3.13.16, HA 2025.12.5 und Fixture-Pin 0.13.301 entsprechend der bestehenden CI.

| Prüfung | Ergebnis |
| --- | --- |
| Neue Transaktionskopplungs-Tests | 66 bestanden (16,37 s) |
| Gesamte Native-Harness-Suite | 807 bestanden (108,79 s) |
| Vollständige pytest-Suite | 1.603 bestanden (159,01 s), keine Fehler/Skips |
| Ruff | Bestanden |
| Formatprüfung | Bestanden, 71 Dateien |
| Python-Kompilierung | Bestanden |
| Git-Whitespace-Prüfung | Bestanden |
| HACS / Hassfest | Validierung über die bestehenden GitHub-Actions-Workflows am PR |

Die Tests prüfen unveränderte Checkpoints, Gate-Zustände, Accounting,
Command-/Safety-OFF-Journale und das ursprüngliche Transaktionsprotokoll.
Der Native-Harness verbietet reale HA-Serviceausgabe mit einem Dispatcher-Spy.
Die Cloud-Testausführung benötigt Loopback-Zugriff für die isolierten HA-Fixtures.
Die Persistenztests unterscheiden Schreibfehler vor/nach Speicherung,
verzögerte Bestätigung, Crash und späte Speicherung nach Abbruch. Ein echter
HA-Runtime-Neustart übernimmt ausschließlich historische Receipts, keine Sitzung.

## PR #106: Restart-Lebenszyklus und historische Audit-Recovery

Die Erweiterung bleibt vollständig im Native-Harness. `restart()` beendet die
Adapterinstanz endgültig: neue Instanzepoche, kein Prepare, kein erneuter Versuch.
Auch ein bereits laufender Collector darf anschließend kein konsistentes Prepare
veröffentlichen. Ein abgebrochenes Prepare verbraucht den Adapter ebenfalls.
Eine Ersatzinstanz muss das Journal erneut laden und sämtliche aktuellen
Nachweise erneut sammeln. Die ressourcenbezogene Serialisierung bleibt bis zum
Ende eines noch laufenden Storage-Callbacks belegt; ein Restart gibt diesen
Schreibzugriff nicht vorzeitig frei.

HA-, Gateway-, Koordinator- und Adapterneustarts bleiben getrennte Ereignisse.
Der HA-Neustart wird mit neuen HA-Fixtures beziehungsweise dem Stop-Fence geprüft;
der Gateway-Neustart erzeugt einen neuen FakeGateway mit demselben Store.
Ein neuer Koordinator hat eine neue Boot-ID und wird im Harness als aktueller
Koordinator der Ressource angebunden. Alte Adapter können dann auch während
laufender Prüfungen keine aktuelle Konsistenz mehr bestätigen. Der
Adapterneustart entwertet seine Instanz selbst bei unveränderten Datenwerten.
Die Tests kombinieren Reihenfolgen, wiederholte Restarts und mehrfache Crashes.
Diese Harness-Serialisierung ist keine prozessübergreifende Sperre.

### Ausschließlich lesende Wiederherstellung

`inspect_audit_history` prüft das bekannte Schema, vollständige Eintrags- und
Kontextfelder, Identitäten, Revisionstypen, kanonisch eingebettete JSON-Evidenz,
Hashkette, Vorgängerbindungen und eindeutige Transaktions-IDs. Wiederholte
Request-IDs bleiben in ihrer ursprünglichen Reihenfolge sichtbar: sie sind
Diagnosekorrelation, keine Idempotenzberechtigung für Recovery-Ausführung.
Unbekannte Schemata, unvollständige Einträge, falsche Typen, doppelte Transaktionen
und beschädigte Verkettungen werden abgewiesen, auch bei zustimmendem Witness.
`async_recover_history` liest zusätzlich den getrennt erhaltenen AuditWitness
und verlangt vollständige Übereinstimmung. Es schreibt keinen Store und
rekonstruiert weder Prepare noch Commit.

`HistoricalAudit` enthält ausschließlich historische IDs und Feldunterschiede
zum optional übergebenen Diagnosekontext. Dieser Vergleich macht einen
übergebenen Kontext selbst nicht frisch: aktuelle Evidenz muss weiterhin über
den normalen Prepare-Prüfpfad gesammelt werden. Auch bei keinerlei Unterschieden
bleiben `historical_only=True`, `current_authorization=False`,
`authenticity_proven=False` und `atomic_multi_store_commit=False`.
HA-Epochen im Recovery-Scope, Gateway-Boot/Runtime, Ownership, Safety, Journal,
Witness, Ledger, Teilnehmer, Pumpenressource, Cycle und Operationsumfang bleiben
an die jeweilige Aufnahme gebunden. Historie kann keine neue Berechtigung liefern.

### Erweiterte Crash-Matrix

| Grenze | Restart-/Crash-Verhalten |
| --- | --- |
| Vor Prepare | Pensionierter Adapter weist jeden neuen Versuch ab |
| Während Prepare | Nach Restart oder Abbruch keine veröffentlichte aktuelle Sitzung |
| Evidenz geprüft / vor Commit | Prepare gehört nur zur alten Adapterinstanz |
| Commit-Prüfung / vor Audit-Schreiben | Veränderte Epochen oder Stores verhindern den Receipt |
| Während Storage vor Persistenz | Abbruch bleibt ungewiss; kein automatischer Retry |
| Persistenz erfolgt, Bestätigung ausstehend | Alter Callback kann keinen Witness in einem pensionierten Adapter aktualisieren |
| Nach Audit-Speicherung | Receipt kann persistiert, aktuelle Konsistenz aber bereits falsch sein |
| Nach bestätigtem Commit | Ausschließlich historische Integrität wiederherstellbar |
| Verspätete Bestätigung nach Boot/Restart | Keine Wiederaktivierung; nicht bestätigter Witness bleibt konfliktbehaftet |
| Wiederholte Crashes | Jeder Versuch endet endgültig, keine Bestätigungs- oder Replay-Schleife |

Barrieren und FakeClock steuern alle Nebenläufigkeitsfälle; echte Wartezeiten
werden nicht zur Synchronisation verwendet. Die vorhandenen Safety-OFF-,
Lease-, Teilnehmer-, HA- und Gateway-Races bleiben Teil der Prüfung.

### Multi-Store- und Vertrauensgrenzen

Zusätzlich zum Protokolljournal vergleicht der Adapter das persistierte Ledger
mit dessen geladenem Schema, Binding, Revision und Ereignissen. Ein laufender
Ledger-Schreibvorgang, ein gesperrtes Ledger oder eine abweichende persistierte
Projektion verhindert Konsistenz. Die Tests injizieren partielle Ledger-,
Protokoll- und Audit-Schreibvorgänge in unterschiedlichen Reihenfolgen vor und
nach Audit-Persistenz. Gateway- und Teilnehmeränderungen werden über die
bestehenden vollständigen Evidenzvergleiche erfasst.

Es existiert weiterhin **keine gemeinsame atomare Commit-Grenze** zwischen
Gateway-Journal, Ledger, Recovery-Protokoll, Audit und Teilnehmerrevisionen.
Ein zurückgelesener Audit-Receipt beweist nur dessen lokale Speicherung.
Audit-Witness und Audit-Speicherung bleiben zwei getrennte Schritte. Ein Crash
dazwischen ist konservativ ein Witness-Konflikt, keine nachholbare Freigabe.
Ein Audit-Rollback gegen einen erhaltenen Witness wird erkannt; Gateway- und
Ledger-Rollbacks gegen erhaltene aktuelle Evidenz blockieren ebenfalls.

Ein gemeinsamer Rollback von Audit und Witness kann eine formal gültige alte
Historie ergeben und ist ohne unabhängige Vertrauenswurzel nicht zuverlässig
nachweisbar. Der entsprechende positive Integritätstest behauptet ausdrücklich
keine Authentizität oder Rollbackfestigkeit. Verlorene Witness-Evidenz sperrt die
Wiederherstellungsprüfung vorhandener Receipts. Auch ein gemeinsamer Rollback
aller Stores und Witnesses kann niemals ON- oder Release-Berechtigung erzeugen:
alle Sicherheitsflags und Gates bleiben unabhängig davon geschlossen.

Für eine echte zukünftige Recovery-Auflösung sind zusätzlich zu den oben
aufgeführten physischen und fachlichen Nachweisen eine unabhängige dauerhafte
Vertrauenswurzel, explizite Crash-/Fencing-Verträge und eine nachweisbare
Commit-Grenze beziehungsweise ein konservatives Protokoll für Teilpersistenz
notwendig. Historische Hashketten ersetzen diese Voraussetzungen nicht.


### Validierung PR #106

Baseline: **1.603 Tests**, davon **807 Native-Harness**. **99 neue Tests** in
`test_fake_gateway_restart.py`; insgesamt **1.702 Tests**, davon **906 Native-Harness**.
Python 3.13.16, HA 2025.12.5, HA-Fixtures 0.13.301 und Ruff 0.17.0.

| Prüfung | Ergebnis |
| --- | --- |
| Neue Restart-/Crash-Tests | 99 bestanden (93 plus 6 ergänzende Rollback-/Reihenfolgetests) |
| Gesamte Native-Harness-Suite | 906 bestanden (209,43 s) |
| Vollständige pytest-Suite, abschließender Lauf | 1.702 bestanden (268,68 s), keine Fehler/Skips |
| Ruff | Bestanden |
| Formatprüfung | Bestanden, 72 Dateien |
| Python-Kompilierung | Bestanden (`custom_components`, `tests`, `scripts`) |
| Git-Whitespace-Prüfung | Bestanden, einschließlich neu hinzugefügter Tests |
| HACS | [Bestanden](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40968790327) |
| Hassfest | [Bestanden](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40968790232) |

Der erste vollständige Lauf bei paralleler Suite hatte einen bestehenden
Shot-Engine-Fehler `deadline_exceeded` (1.701 bestanden, ein Fehler in
`test_delivery_diagnostics_keep_attempts_separate_from_successes`). Der Test
bestand isoliert; der abschließende vollständige Lauf ohne parallele Tests
bestand vollständig. Die bestehenden Timeout-Grenzen und die produktive
Shot-/Pumpenlogik wurden dafür nicht verändert.

Die verlinkten HACS-/Hassfest-Läufe validieren Implementierungscommit `174a5de`;
der anschließende Dokumentationscommit ergänzt ausschließlich diese Ergebnisse.
