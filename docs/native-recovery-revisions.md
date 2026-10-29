# Dauerhafte Teilnehmerrevisionen und laufzeitgebundene Recovery-Epochen

Basis: `main` nach PR #98, `8520f64447f762af8e0b3085f86e385567c34ba7`.
Die Verträge aus [Release-Design](native-recovery-release-design.md),
[Teilnehmer-Gates](native-recovery-participant-gates.md),
[Transaktionen](native-recovery-transactions.md) und
[Plänen](native-recovery-plans.md) bleiben diagnostisch und gesperrt.
`native_activation_allowed=False` und `coordination_ready=False` gelten weiter.
Es gibt keine produktive Aktivierung, neue Pumpenkommandos, Scheduler-Aktivierung,
Replay-, Ersatzshot-, Zählerkorrektur-, Service- oder Dashboard-Schnittstelle.

## Vorab untersuchte Persistenzarchitektur

Die folgenden Schreibpfade waren bereits vor dieser Änderung vorhanden. Sie sind
interne native Harness-Komponenten; das produktive Integration-Setup konstruiert
sie weiterhin nicht. Die normale Auto-Cycle-Reset-Persistenz in `__init__.py`
ist ein anderer bestehender Pfad und wird nicht zum Recovery-Teilnehmer erklärt.

| Teilnehmer / vorhandene Store-Identität | Gespeicherte Änderungen | Bestätigung und bisherige Unsicherheit |
| --- | --- | --- |
| `PumpController`, `growassistant_crop_steering.pump_recovery.<entry>` | `_save_record()` / `_write()`: Ziel, Operations-ID, Aktivierungszeit, Deadline, Starting-/Running-/Fault-Zustand; `_stop_locked()` schreibt `None` erst nach beobachtetem OFF und Drain | `_write()` wartet begrenzt auf einen eigenen Hintergrundtask. Ausstehende Writes verhindern einen folgenden Write; Timeout beendet den Disk-Task nicht. Erst erfolgreicher Write erlaubt das bestehende lokale Clear. |
| `_ShotCoordinator`, `growassistant_crop_steering.shot_journal.<entry>` | `_save()`: vollständige Operationshistorie, Reservation **vor** Aktivierung, Starting/Running/Stopping, Lieferunsicherheit, Fault/Interrupted/Completed, Accounting-Intent, Checkpoint, native Zähler und Recovery-/Restartmarker | Ein Hintergrundtask pro Write, `JOURNAL_TIMEOUT`; ausstehende Writes werden nicht überholt. `_execute()` hält bei Fehlern/Timeout Accounting und Recovery gesperrt. HA-Entry-Optionen werden separat geschrieben; Intent/Checkpoint schützen gegen Doppelbuchung, stellen aber keine atomare Mehrdateitransaktion dar. |
| `_NativeCycleController`, `growassistant_crop_steering.native_cycle.<entry>` | `_save()` / `_commit()`: Pending-Intent, Cycle-ID/Phase, Schedule, Flags, Referenz-VWC/Referenz-Cycle, Zähler, vorheriges Ende und Completion-Reasons | `_commit()` speichert erst Pending, aktualisiert danach Optionen/ggf. Shotjournal, speichert zuletzt Nicht-Pending. Accounting-/Cycle-Locks schützen lokale Übergänge. Timeout lässt den Hintergrundwrite laufen; Pending/Fehler blockieren. |
| `_NativeScheduler` | Keine Store-Datei. Task, Timer, Retry-Latch, `_generation`, `_timer_revision` sind flüchtig | Start/Stop-/Timer-Generationen existieren bereits, sind aber keine dauerhaft bestätigten Teilnehmerrevisionen. |

Bestehende Versionsinformationen: HA-Store-Major/Minor `1/1`, Snapshot-Schema `1`,
Operations-IDs, Cycle-IDs, Shot-Generation, Scheduler-/Timer-Generation. Nur das
separate bestehende Recovery-Ledger besitzt bereits eine persistierte
Eventrevision. Diese Ledgerrevision bleibt eigenständig und wird nicht als
Pumpen-/Shot-/Cycle-Revision umgedeutet. Die neuen Revisionen sind Metadaten
**derselben Teilnehmerdateien**, kein zweites Teilnehmerjournal.

Neustartpfade laden zuerst Pumpen-Recovery, dann das Shotjournal, dann den Cycle.
Das bestehende Pumpenverhalten kann ausschließlich das gespeicherte Ziel
abschalten; Shot-Initialisierung markiert offene Zustände konservativ als
unterbrochen und speichert die erhaltene Historie. Accounting-Checkpoint und
Optionen werden verglichen. Cycle-Neustart benötigt weiterhin Review, auch bei
passenden Werten. Keine dieser Initialisierungen stellt ein Recovery-Permit her.

### Die tatsächliche HA-Abschlussgrenze

In HA **2025.12.5** ist normales `Store.async_save()` kein hinreichender
Erfolgsnachweis: `CoreState.stopping` verschiebt Saves; Read-only kann Writes
überspringen; `_async_handle_write_data()` protokolliert `SerializationError` und
`WriteError`, statt sie zwingend weiterzugeben. Cancellation des Awaiters kann
einen bereits gestarteten Executor-Write nicht zurücknehmen.

`_ParticipantStore` benutzt die vorhandene HA-Store-Datei mit
`atomic_writes=True`. HA verwendet dafür `write_utf8_file_atomic` / AtomicWriter
mit Datei- und Verzeichnis-Synchronisation. Der Adapter lehnt Read-only und
Shutdown-Deferral ab, übersetzt Writerfehler vor HAs Log-and-return-Grenze und
verlangt den tatsächlich zurückgekehrten Writer. Eine bloß zurückkehrende
`async_save`-Attrappe ist kein Abschluss. Payload und Metadaten werden in einem
Dateiumschlag atomar ersetzt. Der äußere HA-Store bleibt Version `1/1`.

**Bestätigt bedeutet hier abgeschlossener lokaler atomarer HA-Dateiwrite.**
Es ist keine Zusage gegen defekte Hardware, nicht eingehaltenes fsync,
Dateisystem-/Backup-Rollback oder privilegierte Writer. Es gibt weder
Mehrdateiatomizität noch Compare-and-swap zwischen HA-Prozessen. Ein alter
Prozess mit laufender Executor-IO wird durch diese lokalen Epochen nicht gefenced.
Abweichende HA-Storage-Implementierungen müssen erneut geprüft werden.

## Datenmodell und Versionsschema

Der vorhandene HA-Dateiumschlag erhält ein zusätzliches Feld
`participant_revision`. `data` bleibt unverändert: keine Verschiebung der
Operationsdaten und kein Ersatz der bestehenden Journale. Bei Pumpen-Clear ist
`data=null`; die Revision bleibt im Umschlag erhalten.

```json
{
  "version": 1,
  "minor_version": 1,
  "key": "growassistant_crop_steering.pump_recovery.<entry>",
  "data": null,
  "participant_revision": {
    "schema": 1,
    "entry_id": "<entry>",
    "pump_entity": "switch.example",
    "store_key": "growassistant_crop_steering.pump_recovery.<entry>",
    "journal_id": "<32-character UUID hex>",
    "revision": 3,
    "digest": "<SHA-256 of canonical JSON data>",
    "metadata_digest": "<SHA-256 of all other revision metadata>"
  }
}
```

`JournalIdentity` bindet Entry, Pumpenziel, exakten Store-Key und zufällige,
über Neustarts erhaltene Journal-ID. `JournalReceipt` ist eingefroren. Digest und
Metadatenchecksumme prüfen gemeinsam Daten, Revisionszahl, Schema und Identität.
Sie sind Integritätsprüfungen, keine Signaturen oder Writer-Authentifizierung.
Bool, Float, negative/fehlende Revisionen, unbekannte Schemas, fehlende Felder,
abweichende Bindungen, ungültige UUID und Digestfehler sind kein gültiger Beleg.
Revisionen verschiedener Journale werden niemals numerisch gleichgesetzt.

### Erzeugung und Bestätigung

`JournalRevision` vereinheitlicht ausschließlich die Abschlussverwaltung der
bestehenden `_write`-/`_save`-Pfade. Beim ersten Write eines nachweislich fehlenden
Journals entsteht Revision 1. Jede anschließend erfolgreich bestätigte
**Inhaltsänderung** erhält `vorherige Revision + 1`. Unveränderte Daten lösen
weder einen neuen Write noch eine künstliche Revision aus. A→B→A erhält zwei
neue Revisionen, obwohl der Inhaltsdigest wieder gleich ist.

Der Kandidat wird vor IO erzeugt, aber erst nach dem erfolgreich abgeschlossenen
Write in den bestätigten In-Memory-Beleg übernommen. Während des Writes gibt es
keinen positiven Revisionsnachweis. Parallele Writes werden lokal abgewiesen,
nicht eingereiht. Die vorhandenen Teilnehmer-Task-Guards verhindern insbesondere,
dass ein verspäteter alter Write durch einen neueren Write überholt wird.
Accounting hat keinen eigenen Store: es bindet exakt an den Shot-Beleg.

## Alte Persistenz und Migration

Alte HA-Umschläge ohne Revisionsmetadaten bleiben lesbare historische Daten.
`JournalRevision.status=legacy`; Snapshots geben `revision=None`, `receipt=None`
und ausdrücklich `status=unknown` aus. Ein vorhandenes historisches `data=null`
ist dabei von einer noch nie vorhandenen Datei unterschieden.

Bestehende konservative Restartmarker dürfen weiterhin in ein altes Journal
geschrieben werden, ohne es dabei zu zertifizieren. Es gibt **keine** automatische
Migration, Revisionsnull, neue Journal-ID für alte Historie oder nachträgliche
Bestätigung alter Operationsdaten. Ein zukünftiger expliziter Migrationsvertrag
müsste Provenienz und Review separat behandeln. Dieser Baustein bietet ihn nicht.

Unbekannte/beschädigte Metadaten und ältere/unbekannte Revisionsschemas laden
nicht als frisches Journal. Der strikte Loader prüft den äußeren Umschlag,
umgeht HA-Cache und automatische Migration und benennt beschädigte Dateien nicht
um. Vorhandene `.corrupt*`-Dateien blockieren ebenfalls. Originaldatei,
Operations-IDs und historische Zustände werden nicht repariert oder entfernt.

Eine Zieländerung bindet die alte Journalidentität nicht stillschweigend um.
Neue Snapshots am anderen Ziel haben keinen gültigen alten Beleg; erneutes Laden
mit abweichender Zielbindung scheitert geschlossen. Eine sichere Neubindung ist
ein separat zu entwerfender Vorgang.

## Crash, Timeout und Cancellation

| Grenze | Verhalten |
| --- | --- |
| Vor dem Write | Vorheriger Dateiumschlag bleibt der wiederherstellbare Präfix. |
| IO erfolgreich, vor Rückgabe an den Aufrufer | Neue Datei kann bereits existieren. Der alte Aufrufer besitzt noch keine Bestätigung. Boot darf den validierten Dateipräfix als Historie laden, niemals als laufzeitgültige Freigabe. |
| Nach bestätigtem Write | Neuer Beleg passt zu Payload und Store-Identität. Neuer Boot erzeugt trotzdem neue Laufzeitepochen. |
| Fehler, Timeout oder Cancellation | Der In-Memory-Beleg bleibt für diese Instanz ungewiss und wird nicht positiv ausgegeben. Der Disk-Task wird bei Await-Timeout/Cancellation nicht abgebrochen. |
| Verspäteter erfolgreicher Write | Kein Callback installiert eine Bestätigung oder heilt ein Gate. Erst ein neuer, validierter Boot kann den Dateipräfix als historischen Zustand einlesen. |
| Weiterer bestehender Safety-Write nach Ungewissheit | Darf konservative Daten speichern, aber keine neue bestätigte Revision erzeugen. Der Umschlag trägt `status=uncertain`, den letzten bestätigten Beleg und den neuen Daten-Digest. Neustart lehnt diesen nicht zertifizierten Umschlag ab. |

Der letzte bestätigte Revisionsbeleg bleibt intern erhalten, wird bei
Ungewissheit aber nicht als aktuell ausgegeben. Bestehende Fault-/Recovery-Latches
werden nicht durch Revisionserfassung geändert. HA-Stop oder Controller-Close
widerrufen zusätzlich die laufzeitgebundene Evidenz.

## Typisierte Laufzeit- und Ownership-Epochen

`RuntimeEpochs` enthält getrennte eingefrorene Typen:

- `BootEpoch`: pro HA-Laufzeitobjekt zufällige Identität in `hass.data`, nie gespeichert.
- `ControllerEpoch`: zufällige Identität pro Teilnehmerkonstruktion.
- `PumpResourceEpoch`: gemeinsame Pumpeninstanz-Identität mit Entry und aktuellem Ziel; Shots, Cycle und Scheduler referenzieren dieselbe Pumpenressource.
- `OwnershipGeneration`: lokale Besitz-/Konfigurationsgeneration. Austausch der Ownership-Aussage und Entry-Updates invalidieren alte Beobachtungen, auch ABA. Entry-Updates werden bewusst konservativ behandelt, einschließlich Option-/Accounting-Updates.
- `SafetyGeneration`: lokale Widerrufe, Fault-/Interrupt-/Close-Ereignisse sowie monotoner Writeversuchs-/Scheduler-/Timerfortschritt.

Entry-Update-Listener werden bei expliziter Initialisierung registriert und bei
Close entfernt. Snapshot-Erfassung liest nur bereits verfügbare Zustände und
ändert weder Generationen noch Dateien oder Gates. Entry-Listener laufen auf
der HA-Ereignisschleife; eine positive Koordination darf niemals auf einer
Annahme synchroner/globaler Writer-Isolation beruhen.

HA-Neustart verändert Boot, Controller und Pumpenressourceninstanz. Ein neuer
Controller im selben HA verändert Controller und gegebenenfalls Ressource.
Ownership-Verlust und erneute Übernahme behalten ihre erhöhte Generation auch
bei gleichem Endwert. Revisionen und Writeversuchsgenerationen verhindern
Journal-ABA über Await-Grenzen. Das sind lokale Provenienzbindungen, keine
Sperren für Automationen, Blueprints, direkte Services oder fremde Prozesse.

## Snapshots, Gates, Pläne und technische Transaktionen

Die Teilnehmer erzeugen ihre eigenen Snapshots mit **Snapshot-Schema 2**,
HA-Store-Schema 1, Revisionsschema 1, `JournalReceipt` oder `unknown` sowie
`RuntimeEpochs`. Schema-1-Snapshots bleiben historische Diagnosen; eine alte
bloße Ganzzahl ist kein neuer Revisionsnachweis. Die Validatoren prüfen den
Beleg gegen Payload und teilnehmerspezifischen Store-Key. Alle Snapshots sind
rekursiv unveränderlich; ihre Erfassung startet keine IO oder Bewässerung.

`SchedulerObservation` hat Schema 2 und die lokale Laufzeitbindung samt
Admission-/Timer-Generation. `EvidenceRevision` für Scheduler besitzt ausdrücklich
**keine dauerhafte Revision und keinen Journalbeleg**. Lokale Ruhe und Generation
ersetzen keinen globalen Quiesce-/Admission-Vertrag.

`GateRequest` Schema 2 bindet Boot, exakten Versuch und vollständige
`EvidenceRevision`s. Diese enthalten Inhaltsdigest, tatsächlichen Journalbeleg,
Laufzeitbindung und beim Scheduler die flüchtigen Generationen. Teilnehmernachweise
führen ihre eigene Revision und Laufzeit mit. `request_current()` vergleicht
jetzt zusätzlich alle aktuellen Teilnehmerrevisionen und Epochen; Aggregation
prüft ihre Übereinstimmung. Gleichwertige Requestkopien sind weiterhin keine
ausgegebenen Requests. Alte Boot-/Controller-/Ownership-/Journalnachweise werden
nicht erneut aktuell. Die bisherige abschließende synchrone Erfassung und alle
negativen Findings bleiben erhalten.

`_RecoverySource.capture()` vergleicht Payload **und** Revisionsmetadaten aus dem
bestehenden Dateiumschlag. Revisionskonflikte werden als zusätzliche Findings
vereinigt; sie verdecken keine Accounting-, Cycle- oder historischen Fehler.
File-only-Änderungen benötigen weiterhin erneute IO-Erfassung; ein synchroner
lokaler Guard kann sie nicht ausschließen. Schema 1 und 2 historischer
Ledger-Evidenz bleiben replaybar, ohne Laufzeitbestätigungen wiederherzustellen.

`ReconciliationPlan.source_revisions` enthält neben Schema/Revisionsbeleg auch
die jeweilige Laufzeitbindung. Vorhandene Evidenzdigests und technische
`read_only_observation`-Transaktionsreceipts binden diesen vollständigen Plan
bereits; die bestehende Revalidierung vergleicht ihn über jede Await-Grenze.
Auch der Planer behält den Check `durable_participant_revisions` bei, solange
ein Teilnehmerbeleg fehlt oder widersprüchlich ist. Es gibt keinen zweiten
Receipt-Store, neue Transaktionszustände oder neue Votes.
Gespeicherte technische Receipts bleiben ausschließlich historische Diagnostik.

`durable_participant_revisions_unproven` entfällt nur, wenn alle vier dauerhaften
Nachweistypen (Pump, Shots, Accounting, Cycle) ihre passenden bestätigten Belege
besitzen. Andere Einzelbefunde bleiben unabhängig bestehen. Insbesondere bleiben
`ownership_epoch_unproven`, `external_writers_unproven` und
`coordinated_resolution_unimplemented` bestehen. Kein Vergleich macht
`coordination_ready` oder `native_activation_allowed` wahr.

## Verbleibende Voraussetzungen

Positive Koordination benötigt weiterhin wirksame Writer-Isolation/Fencing,
physisch unabhängiges OFF, globales Admission-/Drain-Protokoll, geprüfte fachliche
Disposition möglicher Teilbewässerung, explizite neue Baseline und atomar
vereinbarte Accounting-/Optionsprovenienz. Anschließend wären koordinierter
Intent, erneute Teilnehmerprüfung, crashsicherer Commit und eine gesonderte
Freigabeentscheidung mit finaler Live-Prüfung nötig. Diese Arbeit implementiert
keinen dieser Freigabeschritte und setzt keine privaten Sicherheitsflags zurück.

## Validierung

Baseline nach PR #98: **1.207 Tests**, davon **411 Native-Harness-Tests**.
Abschließende lokale Läufe erfolgten sequenziell auf Python 3.13.16,
HA 2025.12.5, pytest 9.0.0 und Ruff 0.17.0, ohne konkurrierenden pytest-Prozess.

| Prüfung | Ergebnis |
| --- | --- |
| Vollständige Native-Harness-Suite | **465 bestanden**, 74,79 s |
| Gesamte pytest-Suite | **1.261 bestanden**, 129,82 s; keine Fehler/Skips |
| Neue Revisions-/Epochenfälle | **54 neue Tests**; 1.207 + 54 = 1.261 und 411 + 54 = 465 |
| Ruff (`custom_components tests scripts`) | Bestanden, Exit 0 |
| Formatprüfung (`custom_components tests scripts`) | Bestanden, 61 Dateien, Exit 0 |
| Python-Kompilierung (`compileall`) | Bestanden, Exit 0 |
| Git-Whitespace einschließlich neuer Dateien (`diff --cached --check`) | Bestanden, Exit 0 |

Die unveränderten bestehenden GitHub-Workflows auf Code-Commit
`7841858f839c1ce2a49b22221327feae7289d3c4` sind ebenfalls erfolgreich:

| Remote-Validierung | Ergebnis |
| --- | --- |
| [HACS](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40851339942) | **Bestanden** |
| [Hassfest](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40851339938) | **Bestanden** |

Die PR-Workflows prüfen den vollständigen finalen Branch erneut.
Der abschließende Fetch bestätigte `main` unverändert bei `8520f64`.
Die HA-Memory-Store-Fixture bildet den zusätzlichen äußeren Umschlag ausdrücklich
ab. Im Native-Harness hält `_participant_envelopes` dessen Metadaten neben der
bisherigen Test-Payloadansicht; dies ist ausschließlich die In-Memory-Darstellung
der **gleichen** Dateiumschläge, kein zusätzlicher produktiver Store. Bestehende
Crash-Fixtures kennzeichnen alte unrevidierte Daten ausdrücklich; Barrieren
reichen Writes nach Freigabe an den tatsächlichen Testwriter weiter. Der reine
Snapshot-Vertragstest verwendet nun vollständige Schema-2-Modellbelege anstelle
einer frei gesetzten Ganzzahl. Bestehende Sicherheitsassertions bleiben erhalten.

Die neuen Tests verwenden deterministische IO-Barrieren, feste FakePump-Ziele,
verbotenen echten HA-Service-Dispatch, vollständige Crash-Checkpoints sowie
reale HA-Dateischreibvorgänge in temporären Testverzeichnissen.
