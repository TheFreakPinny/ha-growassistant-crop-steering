# Native Recovery Gap-Backlog — PR #108

Basis und Evidence-Level: [Coverage-Matrix](native-recovery-safety-coverage.md).
Die IDs `G01`–`G15` sind stabil. **8 P0, 4 P1, 2 P2, 1 P3**.
Ein geschlossenes Softwareticket oder niedrige Risikoeinstufung erteilt keine
Produktivfreigabe. Es gibt keine gemessenen Ausfallwahrscheinlichkeiten; die
Exposition ist qualitativ, nicht als statistische Zuverlässigkeit zu lesen.

## Bewertungsmaßstab

- **P0:** Sicherheitskritisch; vor jeder produktiven Native-Freigabe zwingend.
- **P1:** Wichtige technische Absicherung; vor Nutzung des betroffenen Pfads
  erforderlich. P1 bedeutet keine Erlaubnis, den Pfad ohne Nachweis zu aktivieren.
- **P2:** Zusätzliche Härtung, ohne bestehende Blocker zu ersetzen.
- **P3:** Dokumentation/Verbesserung und fortlaufende Nachweispflege.

Auswirkung beschreibt das mögliche Schadensbild bei späterer Aktivierung.
Heute sind Native-Admission und Release gesperrt. Aufwand S/M/L/XL bedeutet
kleine lokale Änderung / mehrere Komponenten / systemübergreifend /
Hardware plus verteilte Architektur; keine Aufwandsschätzung in Personentagen.
Erkennbarkeit „gering“ heißt schwer zuverlässig zu erkennen.

## Priorisierte Aufgaben mit Abschlussnachweisen

| ID / Priorität | Anforderungen | Lücke und Auswirkung | Exposition / Erkennbarkeit | Abhängigkeiten | Aufwand / Testumgebung | Erforderlicher Abschlussnachweis |
| --- | --- | --- | --- | --- | --- | --- |
| **G01 / P0** | NRS-001/002/007/030 | Keine exklusive geräteweite Ownership. Fremde HA-Automation, Integration, Cloud, Taster oder Geräteclient kann die Pumpe parallel schalten; unkontrollierte Förderung. | Bei Migration/mehreren Clients plausibel; HA-Events sehen Bypässe nicht, Erkennbarkeit gering. | Hardware-/Kanalidentität und vollständiges Pfadinventar; G03. | XL; reale Installation, getrennte Clients und Credentials, Netz-ACLs. | Alle direkten/indirekten Schreibwege inventarisieren; technisch sperren und mit negativen Zugriffstests inkl. Cloud/Boot/manuellen Pfaden belegen. Entity-ID und Ruhe im HA-Bus reichen nicht. |
| **G02 / P0** | NRS-003/005/023 | Software-OFF beweist weder stromlosen Ausgang noch Wasserstillstand. Klebendes Relais, Siphon und Nachlauf bleiben unbeherrscht. | Bei Hardware-/Hydraulikfehler relevant; Erkennbarkeit ohne unabhängigen Sensor gering. | Kanalzuordnung, unabhängiges Messkonzept, G01/G03. | XL; strom-/kontakt-/flussüberwachter Pumpenprüfstand, Not-Aus. | Frisches qualifiziertes Feedback nach letztem möglichen ON und Queue-Drain; Relaiskleben, fehlendes/falsches/stales Feedback, Nachlauf und unabhängigen Abschaltpfad testen. Grenzwerte installationsbezogen dokumentieren. |
| **G03 / P0** | NRS-005/006/016/022/023/029 | Fencing nur im Fake. Kein authentifizierter Enforcer am letzten physischen Schaltpunkt, kein nachgewiesener autonomer Watchdog. HA-only-Restart fenced gültige Gateway-Lease nicht unmittelbar. | Verzögerungen, Partitionen, Eigentümerwechsel; Erkennbarkeit vor tatsächlicher Schaltung gering. | G01, monotone Authority, G04, authentifizierter Transport. | XL; Aktor-/Gatewayprototyp, reale verzögerte Queues, getrennte Prozesse und Split Brain. | Alte ON bei Annahme, Queueentnahme und letzter Ausführung verweigern; Boot/Lease/Deadline/Stopgeneration prüfen. HA-Ausfall ohne Gatewayneustart, Uhrfehler, abgelaufene Lease, Watchdog ohne Poll-Aufrufer und konkurrierenden alten Controller testen. |
| **G04 / P0** | NRS-004/014/015/025/026/028 | Lokale Completion, Hashketten und gemeinsam restaurierbare Witnesses sind kein unabhängiger Anti-Rollback-/Power-Loss-Nachweis. Bestätigte Historie kann verloren gehen oder auf gültigen alten Präfix zurückfallen. | Restore, Medien-/Controllerfehler; gemeinsamer Rollback prinzipiell unerkennbar ohne externe Wurzel. | Storage-/Trustmodell; unabhängiger persistenter Witness außerhalb gemeinsamen Fehlerbereichs. | XL; reale Datenträger, Stromunterbrechung, Backup/Restore und unabhängiger Witnessdienst. | Fehlerdomänen, fsync-/Flush-Vertrag und Unsicherheitsregeln festlegen; Journalverlust, Torn Writes, Ack-Verlust, Journal/Witness-Einzel- und gemeinsamen Rollback prüfen. Keine Signatur-/Authentizitätsbehauptung aus Hash allein. |
| **G05 / P0** | NRS-013/017/018/027 | Keine gemeinsame Multi-Store-Commit-Grenze. Koordinierte Doppelreads und lokale Serialisierung können partielle Fortschritte nur beobachten; kein atomarer globaler Zustand. | Crash während mehrerer Writes / konkurrierender Writer; teils erkennbar, gemeinsame alte Zustände nicht zuverlässig. | G04, G06, Teilnehmerverträge aus G07. | XL; reale Stores/Executor, Prozesskills und vollständige Präfixmatrix. | Konservative Commit-/Reconciliationstrategie spezifizieren und umsetzen; gemeinsame Admission bleibt bei jedem Teilfortschritt gesperrt. Jeder Teilnehmer quittiert eigene Revision; ungewisse Writes werden niemals automatisch als Commit adoptiert. |
| **G06 / P0** | NRS-009/010/011/017 | Teilförderung, Accounting und Cycle-Disposition besitzen keine sichere Auflösung. Keine valide neue Baseline aus unbekannter Wassermenge; riskante Ersatzshots/Doppelbuchung. | Nach jedem unterbrochenen Shot; Unsicherheit sichtbar, tatsächliche Menge unbekannt. | G02, G04/G05, G10; agronomische Entscheidung. | L; Controllerharness plus Mengenmessung und explizite fachliche Reviewfälle. | Historie unverändert lassen; unbekannte Menge erhalten, participant-eigene Disposition und separate neue Baseline mit belegter Provenienz erstellen. Operationen nicht reaktivieren; keine Ersatzshots oder automatisches Replay. |
| **G07 / P0** | NRS-018/019/020 | Teilnehmereigene Gate-Prepare-/Commit-/Revokationsverträge und gemeinsame Gate-Auflösung fehlen. Ein technischer Audit-Commit kann keinen Release erlauben. | Bei jedem Versuch einer künftigen Freigabe; heutige Blocker sichtbar. | G01–G06, G09/G10. | XL; isolierter Harness zuerst, anschließend reale Teilnehmeradapter. | Alle fünf Teilnehmer besitzen eigene Verträge; gemeinsamer Barrier und synchrone Revokation nach letztem await und bei jedem Consumer. Teilauflösung bleibt gesperrt. Separate authentifizierte zweite Entscheidung; kein restaurierbares Permit. Dieser Audit-PR implementiert nichts davon. |
| **G08 / P0** | NRS-012/034 | Keine sichere Wiederaufnahme nach Recovery implementiert/validiert. Explizites Harness-`async_start` ist kein Resume-Protokoll. Unbeabsichtigte neue Bewässerung möglich, wenn später falsch verdrahtet. | Bei künftigem Recovery-Release oder Reload; lokale Latches erkennbar, falsche Gesamtzulassung nicht bewiesen. | G06/G07, frische Sensor-/Budget-/Zeitfensterprüfung. | L; Scheduler-/Lifecycleharness, dann reale Anlage ohne automatische Freigabe. | Gate-Auflösung startet nichts. Gesonderte Admission prüft neue Baseline, Drain/Tray, Budget, Zeitfenster, Ownership und Generation. Negative Tests für Restart/Timer/Events/Teilauflösung; eigenständiger Aktivierungsreview. |
| **G09 / P1** | NRS-006/021/026/033 | Harness-Kontexte und In-process-Identitäten sind keine öffentliche HA-Authentifizierung. Gefälschte Claims oder Replay könnten spätere Reviews beeinflussen. | Heute keine öffentliche Grenze; bei neuem Adapter hoch relevant; Prüfung ohne Authvertrag unzureichend. | HA-Berechtigungsmodell, G01/G03; vor jedem öffentlichen Recovery-Service zwingend. | L; HA-Auth-/Permissionintegration und adversarielle Clients. | Identität ausschließlich aus authentifiziertem HA-Kontext, explizite Berechtigungen und Bindungen; Replay-/Cross-Entry-/Cross-Boot-/gefälschte Actor-Tests. Keine vom Aufrufer akzeptierte `TrustedReviewContext`-Autorität. |
| **G10 / P1** | NRS-007/035 | Gateway-Command/Receipt bindet keine nachgewiesene native Operation-/Cycle-Provenienz; Wasser könnte falsch zugerechnet werden. | Jeder spätere Adapter; fehlende Provenienz heute explizit blockierend. | G01/G03, Operationsidentität, G06; vor positivem Plan erforderlich. | L; durchgängiger isolierter Gateway-/Shot-/Cycleharness. | Kanonische Identitätsbindung, vollständige Mengen-/Teilzustandsprovenienz; falscher Cycle/Shot, Duplicates, Delays und umgeordnete Receipts bleiben blockiert. |
| **G11 / P1** | NRS-008 | HA-Empfangszeit, Plausibilität und Spread erkennen keinen glaubwürdig falschen, wiederholten oder falsch kalibrierten Sensorwert. | Reale Sensoren/Integrationen dauerhaft exponiert; glaubwürdige Falschwerte schwer erkennbar. | Quellprotokoll, Kalibrierung und physische Plausibilitätsgrenzen. | L; Sensorprüfstand und Replay-/Störsignale. | Messzeit/Sequenz/Qualität authentisch binden, Replay und stuck-but-fresh testen; unabhängige Plausibilität dokumentieren. Schutz darf Safety-Gates nicht lockern. |
| **G12 / P1** | NRS-013/024 | Crashharness rekonstruiert Objekte an 14 Grenzen; echte Prozess-/Threadrennen, Netzwerkqueues und abruptes Killen sind ungetestet. | Ausfälle an nicht instrumentierten Grenzen möglich; Modellbeobachtbarkeit hoch, reale unbekannt. | G03–G05, reale Adapter. | L; Mehrprozess-Testumgebung mit Kill/Partition/Delay. | Systematische Präfixliste pro realem Write/Dispatch/Ack, alte Prozesse parallel erhalten, abgeschnittene Speicherstände rekonstruieren; Ergebnisse detected/uncertain/unobservable getrennt berichten. |
| **G13 / P2** | NRS-032 | Kein Worst-Case-Latenznachweis unter HA-Loop-/Executorlast; In-Memory-Audit wächst mit Historie. Deadline-Tests sind keine harte Echtzeitgarantie. | Große Historie/Last; Überschreitung erst nachträglich sichtbar. | G03: Hardwaregrenze bleibt P0 und wird durch P2-Messung nicht ersetzt. | M; Last-/Langzeittest und extern gemessene Laufzeit. | Journal-/Eventgrößen und Looplatenz beobachten; Lastprofile, Stoplatenz und Grenzen dokumentieren. Keine verlängerten Sicherheitsfristen zum Testbestehen. |
| **G14 / P2** | NRS-002/026 | Begrenzte Eventhistorie und lokale Auditdiagnosen liefern keine vollständige Ende-zu-Ende-Beobachtbarkeit/Retention. Unsichtbarer fehlgeschlagener Write bleibt ohne Telemetrie. | Monitoringlücke/Überlauf und lange Laufzeit; teils nur sticky gap sichtbar. | G04/G09, Betriebs-/Retentionkonzept. | M; Langzeit-, Überlauf- und Telemetriefehlerharness. | Fehlende Beobachtung bleibt Blocker; separater Fehlerkanal, Retention und Auditexport testen. Audit darf OFF nicht verzögern und verlorene Events nie als Ruhe ausgeben. |
| **G15 / P3** | NRS-031, alle IDs | Nachweismatrix kann bei neuen Codepfaden veralten; punktuelle Lifecycletests erfassen nicht jede dynamische Konstruktion. | Jede spätere Architekturänderung; über Review/Guardtests gut erkennbar, aber nicht vollständig. | Alle Folge-PRs. | S; statischer Guard, Lifecycletests und Review. | Zwei Guards dieses PRs schließen Import-/Konstruktionsregressionen teilweise; offene Restaufgabe: jede Änderung mit stabilen IDs, konkreten Tests, Evidence-Level und realen Artefakten aktualisieren. Keine automatische Hochstufung anhand Testzahl. |

## Empfohlene Folge-PRs und Abhängigkeiten

Die Nummern bezeichnen Reihenfolge, keine vorab zugesagten GitHub-PR-Nummern.

1. **Enforcement-/Messprototyp ohne produktive Aktivierung:** G01/G02/G03,
   unterstützt durch G11. Inventar, physische Identität, echter letzter Schaltpunkt,
   autonomer OFF und unabhängiges Feedback. Bei ungeeignetem Aktor stoppen.
2. **Durable Trust & Crash Storage:** G04, dann G05/G12. Unabhängiger Witness,
   reale Fehlerdomänen und Multi-Store-Protokoll. Hardwaremessung parallel planen;
   keine positive Recovery aus bloßem Persistenzfortschritt.
3. **Operations-Provenienz und Disposition:** G10/G06; echte Mengenunsicherheit,
   unveränderte historische Buchungen, explizite neue Cycle-Baseline.
4. **Participant-eigene Verträge und Auth:** G09/G07 erst nach den obigen
   Voraussetzungen. Zuerst isoliert, vollständig negativ und mit allen Crashpräfixen.
5. **Separate Scheduler-Admission:** G08, zusätzliche Last-/Beobachtbarkeit G13/G14.
   Kein Start durch Review, Commit, Release-Receipt oder bloßes Gate-Resolving.
6. **Eigenständiger Installations-/Aktivierungsreview:** nur mit geschlossenen
   relevanten Lücken und überprüfbaren Realnachweisen. G15 in jedem PR pflegen.

## No-Go

Jede offene P0-Lücke verhindert produktive Native-Aktivierung. Ebenfalls No-Go:
fehlende Authentifizierung eines neu exponierten Pfads, ungeklärte Provenienz,
unbekannter Pumpenzustand, fehlender frischer OFF-/Exklusivitätsbeleg, ungewisser
Write, Restore ohne unabhängige Vertrauenswurzel, fehlende Teilnehmerquittung
oder ein Schedulerstart aus Recovery heraus. Ein P1/P2/P3-Label löst keinen
existierenden Blocker auf. Es gibt keine Freigabe durch Risikoscore.
