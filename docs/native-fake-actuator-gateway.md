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
| Neue Fake-Gateway-Tests | **53 bestanden**, 0,50 s |
| Vollständige pytest-Suite | **1.405 bestanden**, 130,69 s; keine Fehler/Skips |
| Native-Harness | **609 bestanden**, 78,48 s; separater abschließender Lauf |
| Ruff | Bestanden |
| Formatprüfung | Bestanden, 67 Dateien |
| Python-Kompilierung | Bestanden |
| Git-Whitespace-Prüfung einschließlich neuer Dateien | Bestanden (`git diff --cached --check`) |
| HACS / Hassfest | Ergebnisse der unveränderten PR-Workflows folgen |

Gesamt: **1.352 Baseline + 53 neue Tests = 1.405**. Native-Harness:
**556 + 53 = 609**. Der erste separate Harness-Lauf hatte 608 Erfolge und einen
Timeout im bestehenden Test `test_scheduler_p0_p1_p2_p3_with_independent_budgets`
(dessen Drei-Sekunden-Wartefrist auf den P3-Zähler). In der nachfolgenden
vollständigen Suite bestand dieser Test unverändert. Keine bestehenden Fristen,
Sicherheitsassertions oder Produktivfunktionen wurden dafür angepasst.
