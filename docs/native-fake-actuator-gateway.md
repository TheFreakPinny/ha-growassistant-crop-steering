# Isolierter Fake Actuator Gateway

PR #102 baut auf `main` nach PR #101 (`7a68275`) auf. Das Modell liegt ausschließlich
in `tests/native_harness/fake_actuator_gateway.py`; seine Tests stehen in
`test_fake_actuator_gateway.py`. Kein produktiver Setup-, Service-, Pumpen-, Shot-,
Cycle- oder Scheduler-Pfad importiert oder konstruiert es.

## Vertrauensgrenze

Das Gateway besitzt den einzigen im Modell vorhandenen Ausgang: einen fest
implementierten In-Memory-Aktor ohne Adapter, Netzwerk, HA-Service, MQTT, Modbus
oder GPIO. Das Fake-Persistenzobjekt bewahrt diesen Ausgang über Gateway-Boots,
so dass ein Restart denselben simulierten Pumpenzustand abschaltet. Der Ausgang
wird nicht als aufrufbarer Transport injiziert. Test-Hooks simulieren ausschließlich
Wartepunkte vor der atomaren Ausgabe und vor ihrer Bestätigung.

Die Datenverträge `ActuatorIdentity`, `OwnershipEpoch`, `OwnershipLease`,
`FencingToken` und `CommandIntent` sowie ihre Konsistenzvalidatoren werden
unverändert wiederverwendet. Entscheidend sind zusätzlich die **intern erteilte**
aktuelle Lease, der aktuelle Gateway-Boot und die eigenen Sequenzstände. Der
Aufrufer kann keine Lease als Autorisierungsbeleg zur Ausführung übergeben.
Formal konsistente Vertragswerte allein erlauben kein ON.

Dies ist ein kooperatives, typisiertes Einzel-Eventloop-Modell. Das Fixture besitzt
vertrauenswürdigen Zugriff auf Fake-Uhr und Fake-Speicher für Fehlerinjektion.
Python-Objektzugriff schützt nicht gegen bösartigen Code im selben Prozess;
Writer-Namen und Tokens sind keine kryptografischen Credentials. Das Modell
beweist keine physische Exklusivität oder reale Pumpenberechtigung.
`native_activation_allowed=False` und `coordination_ready=False` bleiben an
Gateway und allen Receipts erhalten. Es gibt keine Recovery-Freigabe.

## Zustandsautomat

| Zustand | Eintritt | Zulässiger nächster Schritt |
| --- | --- | --- |
| Gesperrt/OFF | Boot, Entzug, Sicherheitsstopp, Verbindungsverlust, Timeout | Explizites `recheck()`, danach neue Lease |
| Gesperrt/unbekannt | OFF nicht bestätigbar | Weitere OFF-Versuche; keine Lease bis OFF im Modell bestätigt ist |
| Bereit/OFF | Erfolgreiches explizites Modell-Recheck | `grant()` für genau einen Writer |
| Lease aktiv/OFF | Persistierte neue Generation und ausgegebene Lease | Annahme und Ausführung gültiger Intents |
| Lease aktiv/ON | Atomare simulierte ON-Ausführung | Normaler OFF oder Sicherheits-OFF; weiteres ON wird abgelehnt |
| Fehlergesperrt | Uhrfehler, Persistenzfehler, unterbrochene Ausführung | OFF versuchen; keine Freigabe im betroffenen Gateway-Objekt |
| Veralteter Boot | Ein neues Gateway übernimmt denselben Fake-Speicher | Kein ON und kein Schreiben ins neue Journal; OFF bleibt möglich |

`recheck()` prüft ausschließlich den Fake-Ausgang und die lokale Modellgesundheit.
Es löst keine vorhandenen Pumpen-, Shot-, Cycle-, Recovery- oder Accounting-Gates.
Ein Fehlerlatch lässt sich darüber nicht zurücksetzen. Ein gesunder Neustart
benötigt wieder explizites Recheck und eine frisch erteilte Lease.

## Lease, Ownership und Fencing

`grant(owner, ha_boot, duration)` ist die einzige Erteilungsstelle. Es verlangt
Bereitschaft, OFF, eine endliche positive Dauer und keine bestehende Lease.
Die Generation wird vor der Ausgabe atomar im Fake-Speicher erhöht. Eine zweite
Lease wird abgelehnt. Für einen Ownership-Wechsel sind Entzug/OFF, Recheck und
eine neue Erteilung erforderlich. Auch derselbe Writer erhält eine neue Generation.

Eine Lease gilt im halboffenen Intervall `[issued_tick, expires_tick)`. Die
injizierte `FakeClock` kennt weder UTC noch Systemzeit. Rückwärtslauf und
nichtendliche Ticks sperren den Gateway. Lease- und Befehlsdeadlines werden bei
Annahme und unmittelbar vor dem Ausgang geprüft. Die Prüfung bindet Aktor,
Gateway-Boot, Generation, Lease-ID, Owner und HA-Boot an den tatsächlichen
internen Zustand. Alte Tokens bleiben nach Übergabe, Entzug oder Restart ungültig.
Heartbeat verlängert niemals eine Lease.

Ein persistenter Boot-Zähler erzeugt im erhaltenen Fake-Speicher neue Boot-IDs.
Eine zusätzliche laufzeitinterne Instanzgeneration sperrt bereits beim
Boot-Versuch das vorherige Gateway, auch wenn der Boot-Commit fehlschlägt.
Ein alter Prozess darf danach kein Journalergebnis des neuen Boots überschreiben.
Der Fake-Speicher muss über einen Restart erhalten bleiben; neuer leerer Speicher
ist eine neue Testinstallation, kein Restore-Verfahren. Speicherverlust, Rollback,
Split Brain über getrennte Speicher und kompromittierte Authority werden dadurch
nicht gelöst und sind keine zulässigen Produktionsfreigaben.

## Befehlsannahme und Ausführung

`accept()` ist synchron und damit innerhalb des einzelnen Eventloops atomar. Es
validiert gegen die letzte angenommene Sequenz und persistiert den kompletten
Intent mit einem `accepted`-Receipt. Während eine Ausführung an einer Barriere
wartet, können weitere Intents angenommen werden. Annahme schaltet keinen Ausgang.

`execute()` nimmt niemals implizit einen neuen Befehl an. Ein eigener Async-Lock
serialisiert tatsächliche Ausführungen. Nach dem optionalen `before_apply`-Wartepunkt
werden Lease, Fencing, Deadline, Stop und Ausführungssequenz erneut geprüft.
Zwischen dieser Prüfung, dem synchronen Intent-Commit und der atomaren simulierten
Ausgabe liegt **kein await**. Es gibt keinen externen Transport mit einer
unkontrollierten nachgelagerten Queue.

Annahme- und Ausführungssequenz sind getrennt und pro neuer Ownership-Epoche
monoton. Wird eine höhere angenommene Sequenz zuerst ausgeführt, bleibt die
niedrigere gesperrt. Der Ausführungslock garantiert kein automatisches Drain oder
FIFO aller angenommenen Intents: ein angenommener Intent wird nur durch einen
expliziten Ausführungsaufruf bearbeitet. Wiederholtes ON auf einen bereits
simuliert eingeschalteten Ausgang wird auch mit anderer Command-ID abgelehnt.

Nach dem optionalen `after_apply`-Wartepunkt wird erneut geprüft. Lease-Ablauf,
Entzug, Stop oder Ownership-Wechsel in dieser Phase führen zu OFF und einem
ungewissen Ergebnis: eine zwischenzeitliche simulierte Aktivierung wird nicht
rückwirkend als ungeschehen behauptet. Task-Abbruch oder unerwartete
Ausführungsfehler versuchen ebenfalls OFF und sperren weitere Aktivierungen.

## Idempotenz und Receipts

Der Fake-Speicher bindet jede angenommene Command-ID dauerhaft an den vollständigen
Intent, über Boots und Ownership-Wechsel hinweg. Ein identischer Retry liefert
nur den gespeicherten Status; anderer Inhalt unter derselben ID wird abgelehnt.
Ein dedupliziertes historisches Ergebnis ist keine aktuelle Autorisierung. Ein
alter `accepted`-Status berechtigt insbesondere nicht zur späteren Ausgabe;
`execute()` revalidiert ihn. Vor der Annahme abgelehnte Intents werden nicht als
angenommene Operationen ins Journal geschrieben.

Das Testmodell verwendet bewusst einen eigenen eingefrorenen `Receipt`, anstatt
den Designvertrag `CommandReceipt` um produktive Erfolgssemantik zu erweitern.
Er bindet Intent, Gateway-Boot, Tick, beobachteten Fake-Zustand und Fehlergrund.

| Status | Bedeutung |
| --- | --- |
| `accepted` | Intent committed, keine Aussage über Ausführung |
| `executing` | Persistierter Zwischenstand vor Ausgabe, noch kein Erfolgsbeleg |
| `executed` | Im Modell ausgeführt und bestätigt, keine physische Aussage |
| `uncertain` | Unterbrechung oder Commit-/Bestätigungsunsicherheit; kein Replay |
| `rejected` | Keine Ausführung dieses Aufrufs; Blocker dokumentiert |

Sicherheits-OFF protokolliert eigene `SafetyReceipt`-Einträge mit Grund, Boot,
Tick und `software_off` beziehungsweise `unknown`. Wiederholte OFF-Anforderungen
liefern erneut Diagnose, erzeugen auf bereits ausgeschaltetem Fake-Ausgang aber
keine zusätzliche Ausgangsänderung. Kein Software-Receipt beweist eine physisch
stromlose Pumpe.

## Fake-Persistenz und Crash-Szenarien

`FakePersistence` ist ein explizit im Test weitergereichtes In-Memory-Journal mit
atomaren synchronen Mutationen. Es ist keine Dateisystemimplementierung und kein
HA-Store. `fail="phase:before"` beziehungsweise `"phase:after"` simuliert einen
Fehler vor oder nach einem atomaren Commit. Phasen sind `boot`, `grant`, `accept`,
`execute`, `finish` und `recover`. Auch ein Fehler nach dem Commit wirft eine
Exception: die aufrufende Seite darf aus einer verlorenen Bestätigung keinen
sicheren Erfolg folgern.

| Crash-/Fehlerpunkt | Modellreaktion |
| --- | --- |
| Vor Intent-Commit | Keine Ausgabe; Gateway fehlergesperrt |
| Nach Intent-Commit, vor Ausgabe | Pending-Intent; bei Restart `uncertain`, niemals automatisches Replay |
| Nach Executing-Commit, vor Ausgabe | Konservativ `uncertain`; keine Wiederholung |
| Nach Ausgabe, vor Ergebnis-Commit | OFF-Versuch; Ausführung ungewiss |
| Nach Ergebnis-Commit, Antwort verloren | Aufrufer erhält `uncertain`; dauerhaft gespeichertes Ergebnis kann existieren, Retry bleibt reine Abfrage |
| Restart mit Pending-Intents | Neuer Boot, alle `accepted`/`executing` werden `uncertain`, Ausgang OFF versuchen, keine Lease |
| Fehler beim Wiederherstellen | Fehlerlatch, OFF versuchen, kein ON |
| OFF nicht bestätigbar | Zustand ausdrücklich `unknown`, gesperrt |

Bereits finale historische Receipts bleiben unverändert. Sie zählen keine Shots,
ändern keine historischen Operationen und liefern keine Ersatzbewässerung. Der
Fake kann keine Exactly-once-Wasserlieferung beweisen; Unsicherheit bleibt gesperrt.

## Priorisierter OFF und Watchdog

`safety_off()` setzt den Stop-Latch und entfernt die Lease **vor** dem synchronen
OFF-Versuch. Er benötigt weder normale ON-Lease noch Ausführungslock noch
funktionierende Persistenz. Eine wartende ON-Ausführung kann deshalb nach ihrer
Barriere nicht mehr durchlaufen. Eine neue Lease wird niemals durch OFF erteilt.
Eine gemeinsame aktorweite Stop-Generation invalidiert auch ausstehende Arbeit
einer neueren Gateway-Instanz, wenn ein verspätetes Sicherheits-OFF der alten
Instanz eintrifft. Die neue Instanz muss ebenfalls erneut prüfen und erteilen.
Die Stop-Wirkung ist keine gewöhnliche Position in einer normalen Queue.

`poll()` simuliert den Watchdog deterministisch. Die Tests bewegen die Fake-Uhr
und rufen ihn explizit auf. Befehlsannahme, Recheck, Erteilung, Heartbeat und
Ausführungsrevalidierung rufen ihn ebenfalls auf. Lease-Timeout, fehlender
Heartbeat, HA-Verbindungsverlust und unbekannter Ausgang führen zum Stop/OFF.
Es existiert kein Hintergrundtimer und kein sicherheitsrelevantes `sleep()`.
Bleibt der Eventloop vollständig stehen, läuft auch dieser Watchdog nicht:
**ein Watchdog im selben Prozess ist kein unabhängiger Hardware-Watchdog**.

## Grenzen und nächste Schritte

Die Garantie am letzten Ausführungspunkt gilt nur für den atomaren In-Memory-
Ausgang im einzelnen Eventloop. Für reale Exklusivität fehlen weiterhin ein
unabhängiger Controller beziehungsweise ein selbst fence-prüfender Aktor,
authentifizierte Writer, exklusive Credentials/ACLs, abgesicherter Boot- und
Generationsspeicher, Rollback-/Split-Brain-Schutz, Transport-Drain am tatsächlichen
Ausgangspunkt, unabhängige Abschaltkette und nachprüfbare physische OFF-Evidenz.
Zusätzlich bleiben die koordinierten Teilnehmer-Commits und die Recovery-
Freigabearchitektur aus dem [Design](native-exclusive-actuator-gateway-design.md)
offen. Keine dieser Lücken wird durch einen grünen Fake-Test geschlossen.

## Validierung

Baseline: **1.352**
Tests, davon **556** im Native-Harness. Die neuen Tests verwenden `asyncio.Event`-
Barrieren und Fake-Zeit; echte HA-Service-Aufrufe bleiben durch den vorhandenen
Harness untersagt. Produktionsdateien, vorhandene Tests und Workflows bleiben
unverändert.

Umgebung: Python 3.13.16, Home Assistant 2025.12.5, pytest 9.0.0,
Ruff 0.17.0; Interpreter `/workspace/growassistant-cloud/venv/bin/python`.
Dependencies entsprechen `.github/requirements-ci.txt`, Fixture 0.13.301 wurde
mit `--no-deps` installiert. `uv pip check` meldet ausschließlich die bereits
bekannte Fixture-Metadatenabweichung HA 2025.12.4 gegenüber dem bewussten
Repository-Pin 2025.12.5. Alle pytest-Läufe erfolgen sequenziell mit
`LITELLM_LOCAL_MODEL_COST_MAP=True` und den erforderlichen Fixture-Berechtigungen.

| Prüfung | Ergebnis |
| --- | --- |
| Neue Fake-Gateway-Tests | **54 bestanden**, 0,56 s |
| Vollständige pytest-Suite | **1.406 bestanden**, 129,52 s; keine Fehler/Skips |
| Native-Harness | **610 bestanden**, 78,47 s; separater abschließender Lauf |
| [GitHub-CI](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40945672228) | Bestanden auf Code-Commit `954447c` |
| Ruff | Bestanden |
| Formatprüfung | Bestanden, 67 Dateien |
| Python-Kompilierung | Bestanden |
| Git-Whitespace-Prüfung einschließlich neuer Dateien | Bestanden (`git diff --cached --check`) |
| [HACS](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40945672199) | Bestanden auf Code-Commit `954447c` |
| [Hassfest](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40945672177) | Bestanden auf demselben Code-Commit |

Gesamt: **1.352 Baseline + 54 neue Tests = 1.406**. Native-Harness:
**556 + 54 = 610**. Der erste separate Harness-Lauf hatte 608 Erfolge und einen
Timeout im bestehenden Test `test_scheduler_p0_p1_p2_p3_with_independent_budgets`
(dessen Drei-Sekunden-Wartefrist auf den P3-Zähler). In der nachfolgenden
vollständigen Suite bestand dieser Test unverändert. Keine bestehenden Fristen,
Sicherheitsassertions oder Produktivfunktionen wurden dafür angepasst.

Die abschließenden gezielten und breiten Läufe prüfen auch das aktorweite Fencing
verspäteter Sicherheits-OFFs nach einem Restart. [PR #102](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/pull/102)
zielt auf `main` und wird nicht automatisch gemergt. Der abschließende
Dokumentationscommit ändert keinen Code; die unveränderten PR-Workflows laufen
auch auf diesem Stand erneut.

## PR #103: Safety Hardening

Dieser Abschnitt verschärft das Modell von PR #102. Insbesondere gilt dessen
Beschreibung eines nach verlorenem Finish-Receipt gespeicherten Ergebnisses jetzt
nur für rohe Journalhistorie: Die Gateway-API liefert daraus **keinen gültigen
Erfolgsnachweis** mehr. Auch ein Neustart hebt Speicherunsicherheit nicht auf.
Produktive Verträge, Recovery-Gates, Scheduler, Zähler und Accounting bleiben
unverändert. Es gibt keinen Transportadapter und keine positive Cycle-Freigabe.

### Analyse der tatsächlichen Grenzen

| Grenze | Bisherige Garantie / erkannte Lücke | Gehärtetes Fake-Verhalten |
| --- | --- | --- |
| Lease-Erteilung | Synchron, eine Lease je Boot; Commit vor Rückgabe. Lokale Generation kann mit Journal zurückgesetzt werden. | Vollständiger Journalvergleich gegen separaten Modell-Witness vor Erteilung; Fehler vor/nach Commit sperrt auch spätere Boots. |
| Ownership-Generation | Erhöhung vor Lease-Ausgabe; kein Schutz gegen Kopie alter Zustände. | Restore von Generation, Boot, Revision oder Records wird erkannt, solange der unabhängige Witness erhalten ist. |
| Annahme/Intent | Persistenter vollständiger Intent; `accepted` bedeutet niemals Ausführung. | Ungewisser Annahme-Commit sperrt ON und bleibt bei Retry ungewiss. |
| Letztes Fencing | Nach dem letzten `await`, dann kein Yield bis zum synchronen Ausgang. | Zusätzlich Prüfung der Speichervertrauenswürdigkeit; normale Commits sind ausschließlich synchron. |
| Aktorausführung | Atomarer In-Memory-ON; kein Geräteprotokoll und keine versteckte Queue. | Unverändert. Historische `events` können ON zeigen, obwohl das aktuelle Fake-Feedback OFF ist. |
| Abschlussreceipt | Crash nach ON lässt `executing`; Finish-Commit kann erfolgt sein, obwohl Antwort verloren ging. | Rückgabe/Retry bleibt `uncertain`, selbst wenn rohe Journalbytes `executed` enthalten. Späte Bestätigung hebt die Sperre nicht auf. |
| Sicherheits-OFF | Stop-Fence und OFF synchron vor Fortsetzung wartender ON. | Danach separater `stop`-Commit; Auditfehler kann weder OFF noch Stop-Fence zurücknehmen. |
| Neustart | Laufzeitgeneration sperrt vorherige Instanz schon vor Boot-Commit; Pending wird ungewiss. | Mehrere Boots erhalten Speicherfehler-Latch; alte Instanzen dürfen kein Journal schreiben. |
| Watchdog | Explizites Polling mit FakeClock; derselbe Eventloop. | Poll prüft zusätzlich Witness/Journal. Kein unabhängiger Timer, kein Schutz bei eingefrorenem Prozess. |
| Ownership-Wechsel | Entzug/OFF, explizites Recheck, neue Generation. | Alte Lease-/Command-/Boot-IDs und ausstehende Ausführung bleiben gefenced; kein Restore kann die Sperre aufheben. |

Gleichzeitige Lease-Aufrufe werden im einzelnen Eventloop synchron linearisiert:
Genau einer erhält die Lease. Der Ausführungslock serialisiert normale Commands;
Sicherheits-OFF nimmt diesen Lock nicht. Ein normaler leasepflichtiger OFF-Command
bleibt Teil des normalen Kanals; die priorisierte Notabschaltung ist `safety_off()`.
Ein später OFF einer alten Instanz darf weiterhin abschalten und invalidiert über
den gemeinsamen Stop-Fence auch einen neuen Writer. Er schreibt keinen alten
Auditstand in das aktuelle Journal. Ein neuer ON braucht danach ein explizites
Recheck und eine neue Lease. Es gibt keine automatische Wiederaufnahme.

### Anti-Rollback-Modell und Vertrauensannahme

`JournalSnapshot` enthält Schema, Revision, Boot, Ownership-Generation, Intents,
Receipts und Stop-Audit. `snapshot()` kopiert diese Daten; `restore()` injiziert
bewusst eine alte oder widersprüchliche Kopie. Aktor, Laufzeitgeneration,
Stop-Fence und **FakeWitness** sind ausdrücklich nicht Teil dieser Kopie.
Der Witness hält den zuletzt vollständig bestätigten beziehungsweise committed
Journalstand und einen sticky Unsicherheits-Latch. Das Modell vergleicht die
vollständigen Werte, nicht nur einen lokalen Zähler. Ein Widerspruch sperrt ON;
ein erkannt unsicherer Restore versucht sofort synchron Fake-OFF.

Dies ist eine **explizite Testannahme**, keine implementierte unabhängige
Vertrauenswurzel: Der In-Memory-Witness muss getrennt von der Journalkopie erhalten
bleiben. Auch der Witness selbst ist kein kryptografischer Beleg. Bei Verlust
oder vollständigem Rollback aller vertrauenswürdigen Zustände kann das Modell
Authentizität nicht beweisen. Dieser Fall muss durch `witness=None` dargestellt
werden und bleibt gesperrt. Ein neuer Witness darf ausschließlich eine leere neue
Testinstallation initialisieren; ein beim Konstruktor übergebener nichtleerer
Journalstand wird nicht automatisch vertrauenswürdig. Wiederherstellen der
ursprünglichen Kopie nach erkanntem Rollback beseitigt den Latch ebenfalls nicht.
Es existiert absichtlich keine Entsperr-API für Speicherunsicherheit.

Ein böswilliger Fixture-Aufrufer kann Python-Objekte einschließlich Witness oder
Aktor verändern und damit das Modell umgehen. Das ist keine beweisbare
Rollback-Erkennung. Ein späterer echter Gateway benötigt eine tatsächlich
unabhängige, authentifizierte und rollbackgeschützte Authority, sichere
Provisionierung und eine definierte Wiederherstellung bei Verlust dieser Authority.
Ein weiterer Zähler im selben Backup erfüllt diese Voraussetzung nicht.

### Crash- und Restart-Matrix

| Unterbrechung | Historische Aussage | Weiteres Verhalten |
| --- | --- | --- |
| Vor Accept-Commit | Keine bestätigte Annahme oder Ausführung | Speicherfehler sperrt; kein ON. |
| Nach Accept-Commit, vor Ausführung | Nur Intent vorhanden | Restart markiert Pending ungewiss; niemals Replay. |
| Nach Executing-Commit, vor ON | Ausführung kann noch nicht stattgefunden haben | Konservativ ungewiss; keine zweite Ausführung. |
| Unmittelbar nach ON, vor Finish | Fake-Eventhistorie zeigt ON; `executing` beweist allein kein ON | Neustart schaltet Fake-OFF, historisches Receipt wird `uncertain`/`unknown`; aktueller SafetyReceipt zeigt nur `software_off`. |
| Nach Finish-Commit, Bestätigung verloren/verzögert | Rohe Journalhistorie kann Erfolg enthalten | Witness sperrt, API liefert `uncertain`; auch nach mehreren Boots und spätem ACK kein gültiger Nachweis. |
| OFF zwischen Intent und Ausgang | Stop-Fence wird synchron erhöht | Wartender ON wird abgelehnt; kein ON nach Stop. |
| OFF nach ON, vor Receipt | Zwischenzeitlicher ON bleibt historisch möglich | Sofort Fake-OFF; Ergebnis ungewiss, kein Replay. |
| Lease-Entzug/-Ablauf oder Watchdog am Wartepunkt | Bisheriger Intent verliert Berechtigung | Vor Ausgang kein ON; nach Ausgang OFF und ungewiss. |
| Boot während laufender Operation | Alte Instanz verliert Laufzeitautorität | Kein Überschreiben des neuen Journals; Sicherheits-OFF bleibt aktorweit möglich. |
| Fehler beim Stop-Audit | OFF und Fence sind bereits wirksam | Weitere ON-Ausführung gesperrt, auch wenn Audit nicht gespeichert werden konnte. |
| Restore / beschädigte Daten / unbekanntes Schema | Keine verlässliche aktuelle Autorität | Sticky Sperre, OFF-Versuch, keine Lease. |

Historischer ON und aktuell bestätigter **Fake-OFF** sind getrennte Aussagen.
Kein Intent, keine Eventliste und kein Receipt bestätigt physische Stromlosigkeit,
Relaisposition, hydraulischen Durchfluss oder gelieferte Wassermenge. Historische
Shot-Zähler und Accounting werden weder korrigiert noch durch Ersatzshots ergänzt.

### Fehler-Injektion und Nebenläufigkeitsgrenze

Alle Commit-Phasen (`boot`, `grant`, `accept`, `execute`, `finish`, `recover`,
`stop`) unterstützen `phase:before`, `phase:after` und `phase:delay`.
`before` wirft vor Mutation, `after` nach atomarer Mutation; `delay` committed,
hält die Bestätigung in `delayed_receipts` zurück und meldet dem Gateway ebenfalls
Unsicherheit. `acknowledge(index)` liefert die historische Bestätigung ohne
Entsperrung. Selbst die deterministisch bekannte Before-Fehlerklasse wird
konservativ gesperrt: Das ist keine Behauptung über reales Storage-Verhalten.

`stage(snapshot)` und `deliver(proposal)` erlauben Tests mit verspäteten und
vertauschten Storage-Vorschlägen. **Bereits das Staging sperrt ON**, weil keine
synchrone eindeutige Commit-Bestätigung vorliegt. Journal-, Boot-/Laufzeit- und Stop-Fence werden im Vorschlag diagnostisch
festgehalten; `deliver()` lehnt jeden asynchronen Vorschlag ab, auch einen mit
passenden Fences. Es wird kein Vorschlag installiert. Damit behauptet das Modell keine funktionierende asynchrone
Persistenz mit Commit-Reordering. Insbesondere wird kein Callback zurückgehalten,
der später hinter einer letzten Fencing-Prüfung doch noch einen Ausgang schalten
könnte. Ein echter asynchroner Store müsste Commit-CAS, Cancel/Drain, dauerhafte
Deduplizierung und späte Writes unabhängig nachweisen; bis dahin bleibt diese
Fehlerklasse fail-closed statt optimistisch simuliert.

Beschädigung wird an der typisierten In-Memory-Snapshot-Grenze injiziert;
es gibt keinen Byte-Parser, keinen Disk-Torn-Write, kein fsync, keinen
Stromausfalltest und keine Aussage über Storage-Hardware. Split Brain über
getrennte Aktor-/Witness-Objekte und beliebige Threads sind nicht abgedeckt.
Die Tests verwenden ausschließlich FakeClock und `asyncio.Event`-Barrieren.

### Voraussetzungen vor späterer Recovery-Anbindung

Es fehlen weiterhin physisches Feedback, exklusive Gerätezugänge, authentifizierte
Writer, Fencing am tatsächlichen irreversiblen Ausgang, eine unabhängige
Abschaltkette und Watchdog, rollbackgeschützte Authority sowie crashsichere
Persistenz-/Transportverträge. Zusätzlich sind vollständige teilnehmereigene
Recovery-Belege, gemeinsame Admission-Barrieren und die Disposition historischer
Teilbewässerung erforderlich; siehe [Gateway-Design](native-exclusive-actuator-gateway-design.md).
Diese Tests liefern keine positive Recovery-/Cycle-Freigabe. Es bleiben
`native_activation_allowed=False`, `coordination_ready=False`; kein Scheduler
startet und es werden keine HA-Services oder echten Pumpenbefehle ausgeführt.

### Validierung PR #103

Baseline nach gemergtem PR #102: **1.406 Tests**, davon **610 Native-Harness**.
Zusätzlich **60 deterministische Safety-Tests**; Fake-Gateway insgesamt **114**,
Native-Harness **670**, gesamte Suite **1.466**. Kein bestehender Test und keine
produktive Datei wurde geändert. Alle Läufe erfolgten sequenziell mit
`LITELLM_LOCAL_MODEL_COST_MAP=True` und Python 3.13.16 im vorhandenen
`/workspace/growassistant-cloud/venv`; Dependencies gemäß
`.github/requirements-ci.txt`, HA-Fixture 0.13.301 mit `--no-deps` wie im CI.

| Prüfung auf finalem Code `d3e38cf5e3e6caf0f4e509d45c8f92930489a6a8` | Ergebnis |
| --- | --- |
| Fake-Gateway (`pytest -q tests/native_harness/test_fake_actuator_gateway.py --tb=short`) | **114 bestanden**, einschließlich 60 neuer Tests |
| Native-Harness (`pytest -q tests/native_harness --tb=short`) | **670 bestanden**, 80,37 s |
| Vollständige pytest-Suite (`pytest -q --tb=short`) | **1.466 bestanden**, 133,99 s; keine Fehler/Skips |
| Ruff (`ruff check custom_components tests scripts`) | Bestanden |
| Format (`ruff format --check custom_components tests scripts`) | Bestanden, 67 Dateien |
| Kompilierung (`python -m compileall -q custom_components tests scripts`) | Bestanden |
| Git-Whitespace (`git diff origin/main --check`, zusätzlich staged vor Commit) | Bestanden |
| [HACS](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40949361705) | Bestanden |
| [Hassfest](https://github.com/TheFreakPinny/ha-growassistant-crop-steering/actions/runs/40949361909) | Bestanden |

Der abschließende Dokumentationscommit ergänzt nur diese Ergebnisse; die
Repository-Workflows prüfen auch diesen Stand erneut. Der Pull Request zielt
auf `main`; kein automatisches Mergen oder Aktivieren produktiver Pfade.
