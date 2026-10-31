# Native Engine: kleine PRs bis zur kontrollierten Erstinbetriebnahme

Basis: `main` e0bc331 nach PR #108. Priorität ist funktionale Integration des
vorhandenen Kerns. Die Bezeichnungen R1–R7 sind Planungs-IDs, keine reservierten
GitHub-PR-Nummern. [Readiness](native-engine-readiness.md) und
[Paritätsmatrix](native-engine-feature-parity.md) begründen die Arbeitspakete.

Dieser Analyse-PR aktiviert nichts. R1–R6 arbeiten zunächst mit simulierten
Pumpen. Nur R7 darf in einem gesondert geprüften Aktivierungsdiff einen realen
HA-Betriebsweg ergänzen. Bis dahin bleiben insbesondere `simulated_pump`,
`native_activation_allowed=False` und vorhandene Recovery-Sperren unverändert.
Ein harter Gateblocker wird nicht durch ein UI-Acknowledgment ersetzt. Externe
Gateways, Hardwareprototypen und neue allgemeine Sicherheitsframeworks sind
keine Abhängigkeit dieser Roadmap.

## R1 — Runtime und Lifecycle zusammensetzen (P0)

**Ziel:** Einen vorhandenen Native-Stack sicher verwalten können, ohne ihn im
normalen Setup zu aktivieren.

**Komponenten:** `__init__.py`, `pump.py`, `shot_engine.py`, `cycle.py`,
`scheduler.py`; ein kleiner entrybezogener Runtimecontainer, Lifecyclefixtures.

**Änderungen:** Bereits vorhandene APIs Pump → Shots → Cycle → Scheduler
zusammensetzen; Konstruktion, recoverywirksame Initialisierung und Schedulerstart
explizit trennen. Normales Setup bleibt diagnostisch/Legacy. Nach Teilfehlern
erworbene Ressourcen schließen; bestehende Unloadreihenfolge erhalten. Bei
unbestätigtem OFF Owner/Fehlerzustand erhalten, Reload darf keine zweite Runtime
erzeugen. Keine zusätzliche Recovery-Protokollkette initialisieren.

**Abhängigkeiten:** keine. **Akzeptanz:** Realer HA-Entry kann einen injizierten
simulierten Stack besitzen und geordnet schließen; normales Setup erzeugt weder
Controller noch Native-Timer oder Pumpenservice. Scheduler bleibt gestoppt.

**Tests:** HA-Setup/Unload/Reload, Fehler nach jeder Initialisierungsstufe,
wiederholtes Close/Cancel, aktiver Shot beim Unload, fehlgeschlagenes OFF,
Ownerfreigabe erst nach bestätigtem Close; bestehende Audit-Guards unverändert.

## R2 — Konfiguration und Steuerungsverantwortung verbinden (P0)

**Ziel:** Genau ein lokaler Executor und ein Resetowner; vorhandene Parameter
und Entity-IDs als einzige Konfigurationsquelle nutzen.

**Komponenten:** Config-/Options Flow, `config.py`, Number/Switch-Entitäten,
`__init__.py` Services/`_CycleResetCoordinator`, Runtimecontainer und Cycle.

**Änderungen:** Für den simulierten Nativepfad ownerabhängiges Routing ergänzen.
Legacy-Tagesreset und Native-Sunrise niemals parallel betreiben. Options-vor-Data,
leere Overrides und bestehende numerische Beschreibungen wiederverwenden.
Parametereingaben gegen Native-Bereiche und 300-s-Pumpenlimit validieren;
Sensorpolicy sichtbar machen oder explizit dokumentierte konservative Werte
nutzen. Pump-/Schedule-/Sensorwechsel invalidieren die Freigabe und stoppen
kontrolliert. Unkritische Änderungen dürfen keine zweite Runtime erzeugen.
Ein Recoveryreview darf keine manuelle Zähler-/Flagänderung als Beweis akzeptieren.

**Abhängigkeiten:** R1. **Akzeptanz:** Gleiches Parametersnapshot bei identischer
Konfiguration; bestehende Entity-IDs und Legacyfunktion bleiben stabil; ein
Native-Reset und ein Shotabschluss können nicht getrennt dieselben Zähler ändern.
Kein produktiver Native-Schalter im Flow.

**Tests:** Optionsflow/Reload mit vorhandenen und gelöschten Helpern, Zustands-
gegen Parameteränderung, Ownerkonflikt, Native- versus Legacyreset, Serviceaufruf
während Accounting, verspäteter alter Callback und Pumpen-Neuzuordnung.

## R3 — Migration als konkrete Zustandsprüfung vorbereiten (P0)

**Ziel:** Eine prüfbare Übergabe statt ungeprüfter Übernahme von Helperwerten.

**Komponenten:** bestehende Configauflösung, Cycle-/Shotcheckpoints,
`native_safety.py`, Diagnostics; Migrationsdokumentation und lokale Fixtures.

**Änderungen:** Read-only Vergleich von tatsächlichen Blueprintinputs mit
Config-Entry-Pumpe, Zeithelpern, VWC/Drain, Parametern, Flags/Zählern/Referenz,
Last-Shot/Typ und Resetmarker. Explizite Abweichungsliste erstellen. Bestehende
Stores und historische Shotdaten nicht ändern. Übergabe bevorzugt vor neuer
Sunrise mit explizit geprüftem sauberem Zustand planen; echtes Last-Shot/Soak
behalten. Falls Übernahme eines laufenden Zyklus nicht eindeutig abbildbar ist,
bleibt sie gesperrt; kein impliziter Nullreset. Blueprint-Instanz und weitere
bekannte HA-Writer konkret benennen; unauflösbare Templates als offen ausweisen.

**Abhängigkeiten:** R2. **Akzeptanz:** Bericht erklärt jeden nicht übernehmbaren
Wert und sämtliche manuellen Schritte. Disable eines Blueprint darf nicht als
Beweis für Ende seiner laufenden Delays gelten. Rückwechsel enthält ebenfalls
Stop, OFF und Zustandsprüfung. Bericht erteilt keine Startberechtigung.

**Tests:** abweichende Blueprintpumpen/-helper, Optionsmaskierung alter Werte,
P1 aktiv/P2-Referenz aus altem Zyklus, fehlender Last-Shot trotz Zählern,
unklare Lieferung, verpasster Sunrise, neue saubere Zyklusgrenze, unveränderte
Historie und null Pumpenserviceaufrufe während der reinen Prüfung.

## R4 — Manuellen Neustart-/Fehlerreview implementieren (P0)

**Ziel:** Nach kontrolliertem Stop oder HA-Neustart wieder bedienbar werden,
ohne Wiederholung eines unklaren Shots.

**Komponenten:** Pump-/Shot-/Cyclelatches, `native_safety.assess_recovery`,
`recovery_revision.py`, Runtimecontainer; schmaler HA-Reviewhandler zunächst
ausschließlich für Simulation. Die alte Release-Protokollkette bleibt gesperrt.

**Änderungen:** Vorhandene Gründe klassifizieren und konkrete Prüfungen zuordnen:
ursprüngliches Ziel OFF, keine pending/late ON-Calls oder Writes, aktuelle
Sensoren, unveränderte Konfiguration und stimmige Checkpoints. Quittierung an
aktuellen Entry, Ziel, Runtimegeneration und Storeversion binden; Änderungen
widerrufen die Prüfung. Reviewentscheidung getrennt und nachvollziehbar speichern,
Originaloperation und Originalaccounting nicht überschreiben. Konsistenten
Restart nur manuell für neue Evaluation freigeben; fehlender Zyklus benötigt eine
explizite neue Zyklusentscheidung. Unsichere Lieferung nicht als Erfolg buchen,
nicht nachholen und nicht durch Counterreset aus dem Budgetgedächtnis entfernen.
Unaufgelöste Speicher-/Accountingfehler bleiben blockiert.

**Abhängigkeiten:** R1–R3. **Akzeptanz:** Ein sauberer simulierter Restart und ein
behobener Sensorfehler können nach Review eine neue aktuelle Entscheidung
erreichen; kein Retry derselben Operation. Unbestätigtes OFF, offene Servicearbeit,
inkonsistente Stores oder unbekannte Verantwortung sind nicht quittierbar. Ein
persistierter Reviewstatus startet nach Neustart niemals automatisch Scheduler.
Kein allgemeines `clear_all_faults` und keine Änderung produktiver Gates.

**Tests:** vollständige lokale Store-Save/Load-/Runtime-Rekonstruktion;
Unterbrechung vor/nach Reservation, ON, OFF, Accountingintent und finalem Save;
Options-/Store-Divergenz, Schreibfehler/Timeout, konkurrierender Stop/Review,
veraltete Quittierung, erneuter Restart, keine Änderung alter Shotrecords,
keine erneute Operations-ID und kein automatischer Ersatzshot.

## R5 — Manuelle Bedienung und Live-Diagnose anschließen (P1)

**Ziel:** Native im simulierten HA-Entry bedienen und Fehler verstehen können.

**Komponenten:** bestehende Services/Buttons, `_NativeCycleController`
Manualmethoden, Schedulerstop, `NativeDiagnosticsCoordinator`, Übersetzungen.

**Änderungen:** Nativeowner routet Start/Complete-P1 durch Cycle, Stop blockiert
neue Reservationen und drainiert laufenden Shot. Reset und Last-Shot-/Counter-
Mutationen unter Nativeowner kontrollieren oder nachvollziehbar ablehnen.
P2 manual bleibt ohne automatische Bewässerung; kein neuer direkter Pumpen-ON-
Service. Runtimezustand, letzter Ausgang, aktive Operation, Schedulerstatus,
Sperrgrund und erforderliche Reviewaktion über bestehende Diagnosekonventionen
anzeigen. Historische und aktuelle Evidenz ausdrücklich unterscheiden.

**Abhängigkeiten:** R2, R4. **Akzeptanz:** Keine Bedienaktion umgeht Shotgates;
Notstop bleibt auch bei blockiertem Review verfügbar. Bestehende Legacyservices
bleiben im Legacybetrieb kompatibel. Sensorreadiness allein zeigt niemals
Aktivierungsbereitschaft an.

**Tests:** echte HA-Serviceregistrierung und Zielauswahl, Manual-P1 und Completion
bei gleichzeitigem Fault, wiederholter Stop, Mutationen während Shot/Review,
Diagnoseupdates und Listenercleanup bei Reload; keine neuen produktiven ON-Pfade.

## R6 — Gesamtablauf gegen Blueprintverhalten abnehmen (P1)

**Ziel:** Die verdrahtete Integration mit simulierten HA-Pumpen abnehmen, bevor
ein produktiver Startpfad ergänzt wird.

**Komponenten:** `tests/lifecycle`, `tests/shot_engine`, bestehender Native-Harness,
Feature-Paritätsmatrix. Kein Ausbau der Fake-Gateway-Architektur.

**Änderungen:** Gemeinsame Szenariodaten für erwartetes bisheriges Verhalten und
Native-Gesamtablauf: P1-Schwelle/FC/Drain, P2-Referenz/Drop/Cap/Soak/Budget,
P3 optional, manual/sensor, overnight/DST, Reload/Restart und Fehler. Bewusste
Abweichungen aus der Matrix als eigene Erwartungen testen, keine falsche
bitgenaue Parität behaupten. Normaler HA-Lifecycle mit injiziertem simuliertem
Stack muss jetzt zusammen mit Bedienung und Review funktionieren.

**Abhängigkeiten:** R1–R5. **Akzeptanz:** Ein vollständiger Zyklus plus kontrollierter
Restart und Recovery ist im integrierten Fixture nachvollziehbar; kein doppelter
Shot, kein Catch-up, nur ein Resetowner; produktiver Normalsetup bleibt inaktiv.
Alle `PARTIAL`-Integrationspunkte anhand konkreter Tests neu bewerten.

**Tests:** vorhandene gesamte Suite/Harness, Fehlermatrix inklusive verspätetem ON,
Storagefehler, Cycle-Reset während Accounting, Sensorstörung während Shot, keine
zweite Runtime nach Reload. Ruff/Format/compile/Whitespace sowie HACS/Hassfest.

## R7 — Kontrollierter erster Nativebetrieb (P0, zuletzt)

**Ziel:** Einen ausdrücklich gewählten HA-Entry nach überprüfter Übergabe an
Native übergeben; Standard bleibt Blueprint beziehungsweise inaktiv.

**Komponenten:** Options-/Aktivierungsflow, Runtimeowner, schmaler Start-/Reviewpfad,
Diagnostics, Migrations-/Rollbackanleitung und betroffene Aktivierungsgates.

**Änderungen:** Eigenständig geprüften produktiven HA-Betriebsvertrag ergänzen.
Bestehende konstant gesperrte Forschungs-/Releasepfade bleiben unverändert; die
genauen notwendigen Anpassungen an Kern-Ownershipgates im Diff offenlegen und
gegen verbleibende Schutzwirkung prüfen. Niemals reale Hardware als simuliert
deklarieren. Begrenzte Startaktion erst nach stillgelegtem Blueprint und allen
bekannten konkurrierenden HA-Writern, abgeschlossenen Delays/Calls, frischem OFF,
stimmigem Zustand, Sensorprüfung und manuellem Review zulassen. Erst danach
vorhandenen Scheduler starten. HA-Neustart und Reload bleiben zunächst gestoppt
und brauchen neue Prüfung; kein automatisches Umschalten auf Blueprint bei Fehler.

**Abhängigkeiten:** R1–R6 vollständig, akzeptierte Matrixabweichungen und konkrete
Installationsprüfung. Die Releaseentscheidung akzeptiert explizit die Grenzen
gegen externe Writer und fehlende physische OFF-Garantien innerhalb dieses
HA-Betriebsumfangs. Falls diese Grenzen am Einsatzort nicht vertretbar sind,
bleibt dieser Einsatz gesperrt; daraus folgt kein neues Hardwareprojekt.

**Akzeptanz:** Der Operator sieht Zielpumpe, einzigen lokalen Owner und Start- /
Sperrzustand. Blueprint und Native können im kontrollierten Übergabepfad nicht
gleichzeitig starten. Freigabe ist an aktuelle Konfiguration gebunden und nach
Restart nicht wiederhergestellt. Erstinbetriebnahme beginnt beaufsichtigt mit
begrenztem Shot; OFF, Zähler, Journal, Soak und anschließender Zustand werden
geprüft. Rollback erst nach Native-Stop und OFF; unklarer Shot bleibt gesperrt.

**Tests:** negative Aktivierungs-/Doppelownerfälle, aktive Blueprint-Delays,
unvollständige Zustandsübernahme, Zielwechsel nach Review, doppelte Startrequests,
Restart/Reload während Start/Shot/Stop, vollständige Regression und CI.
Beaufsichtigter Standorttest separat dokumentieren: lokale Simulation beweist
weder reales Relaisverhalten noch externe Isolation. Dieser Analyse-PR führt
keinen solchen Standorttest aus.

## Wiederverwendung und Reihenfolge

Unverändert wiederverwenden: Entscheidungsregeln, Pumpenprimitive, Reservierung/
Supervision/Accounting, Cycletransitions, Schedulerkoaleszierung/-Timer,
Store-Revisionsschutz, Sensorassessment und bestehende Parameter-/Entitymodelle.
An deren Rändern ergänzen: Owner-/Lifecycleverdrahtung, Services, geprüfte
Zustandsübergabe, manueller Review und Live-Diagnose. Konkrete Anpassungen
erst durch die genannten Akzeptanztests begründen; keine pauschale Entkernung.

R1 → R2 → R3 → R4 → R5 → R6 → R7 ist die empfohlene Reihenfolge. Eine spätere
automatische saubere Wiederaufnahme, neue Profile, feste Intervallplanung,
Historienkompaktierung oder zusätzliche Diagnostik sind eigenständige spätere
Verbesserungen und keine Voraussetzung für diesen kontrollierten ersten Betrieb.
