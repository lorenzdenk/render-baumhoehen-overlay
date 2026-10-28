# Baumhöhen-Overlay

Baumhöhe (Kronenhöhe über Grund) als **MBTiles-Overlay** für LoriMaps: 512-px-Kacheln, WebP mit Transparenz,
identische Kacheln nur einmal gespeichert (gleiches Schema wie `lorimaps-render`).

> **GitHub:** Ein Push auf `render.env`, `regions/` oder die Skripte startet den Workflow „Render Baumhöhen-Overlay“
> (oder manuell mit Eingabe der Region). Ergebnis unter *Releases* (`baumhoehe-<NAME>`); Tasmanien: zuerst „Prepare Tasmania canopy“, dann Render mit Region `styx`, Logs und Vorschaubilder
> in den Branches `render-logs` und `render-logs-<shard>`.

## Daten

**nDOM1** des LGL Baden-Württemberg (Open Data, dl-de/by-2-0): normalisiertes Oberflächenmodell = bildbasiertes
DOM (belaubte Luftbildflüge, alle 2 Jahre) minus DGM (Laserscan). 1-m-Raster, Höhengenauigkeit 40–60 cm, Werte −1 bis 55 m.
Das ist für Baumhöhen besser geeignet als DOM1 − DGM1, weil das DOM1 aus **unbelaubten** Laserflügen stammt.

- Download: `https://opengeodata.lgl-bw.de/data/ndom1/ndom1_32_{E}_{N}_2_bw.zip` – 2 × 2 km, E = ungerade km,
  N = gerade km (UTM 32), darin vier GeoTIFFs 1 × 1 km. Ausserhalb von BW: 404.
- Gleiches Schema für `data/dom1/dom1_…` (GeoTIFF) und `data/dgm/dgm1_…` (XYZ-Text).
- Im nDOM stecken auch Gebäude, Brücken, Fahrzeuge. Ausmaskiert werden mit OSM: Gebäude, Wasser, Brücken,
  Parkplätze, Strom- und Seilbahnleitungen (`scripts/prepare_mask.sh`). Unter 2 m gilt als „keine Krone“.

## Ablauf (`scripts/baumhoehe.py`)

Das Gebiet wird in Blockkacheln der Zoomstufe `ZSPLIT` (11, ca. 13 × 13 km) zerlegt und auf `SHARDS` Rechner verteilt.
Pro Block: nDOM laden → nach Web Mercator (1,6 m bei z15) → Maske → 2-Band-Zwischenraster (Höhe×4, Kronendeckung)
mit gemittelten Übersichten → Kacheln z11–z15 → Block löschen. Damit bleibt der Platzbedarf pro Rechner klein, egal wie
gross das Gebiet ist. Ein Abschlussjob rendert z8–z10 aus den Block-Übersichten und fügt alles zusammen; wird die Datei
grösser als 1,9 GB (Release-Grenze), bleiben es Teile `…-partKofN.mbtiles`.

Darstellung: Farbe = mittlere Kronenhöhe, Deckkraft = Kronendeckung (max. 80 %). Farbskala in `STOPS`
(gelb → türkis → blau → violett, bewusst nicht grün, damit sie sich vom Wald der Grundkarte abhebt).

## Regionen

`regions/konstanz.env` (Test, 4 Blöcke), `regions/bw.env` (ganz BW, 20 Shards). Region in `render.env` wählen.

## Lizenz der Ergebnisse

Baumhöhe: Datenquelle LGL, www.lgl-bw.de (nDOM1), dl-de/by-2-0, Daten geändert. Maske: © OpenStreetMap-Mitwirkende (ODbL).
Steht in den Metadaten jeder MBTiles-Datei.

## Tasmanien (Region `styx`)

Quelle: theLIST **Tree Canopy Height** (aus allen LiDAR-Projekten von Land Tasmania, 2 m, © State of Tasmania,
CC BY 3.0 AU), landesweit als ZIP (~8,4 GB): `https://listdata.thelist.tas.gov.au/opendata/data/LIST_TREE_CANOPY_HEIGHT_STATEWIDE.zip`.
Nur wo LiDAR geflogen wurde. Workflow „Prepare Tasmania canopy“ schneidet einen Ausschnitt aus und legt ihn als
Release `tas-data` ab; `SOURCE=release:tas-data/tas-chm-<name>.tif` in der Region verwendet ihn.

Farbskala `riesen`: unter 50 m zurückhaltend grau-blau, ab 50 m gelb → orange → rot → magenta → violett (100 m).
Bäume ab 50 m werden auf allen Zoomstufen mit ihrer **Maximalhöhe** gezeichnet, verschwinden also auch weit
herausgezoomt nicht im Mittelwert.

## Workflows

| Workflow | Zweck |
|---|---|
| Render Baumhöhen-Overlay | Farbskala-Overlay einer Region (`regions/*.env`), Release `baumhoehe-<NAME>` |
| Archive Tasmania canopy | theLIST-Kronenhöhe einmalig laden → Release `tas-chm` (Kacheln) und `tas-data` (Baumliste) |
| Tasmania giant trees (target style) | Zielscheiben-Overlay der Bäume ab 50 m + Gruppen ab 80 m → Release `riesenbaeume-tasmanien` |
| BW tallest trees / … ranking | Suche nach den höchsten Bäumen in BW (nDOM, Nachmessung DOM1−DGM1) → Release `bw-data`, `data/` |
| Repack release | Teildateien eines Releases zu möglichst wenigen Dateien < 1,9 GB zusammenfassen |

Zielscheiben-Stil (`scripts/zielscheibe.py`): Punkt je Baum ab 50 m (Farbe = Höhe, 50 m gelb → 100 m violett),
Ringe 100/250/500 m um jeden Bestand (Lücken bis ~500 m geschlossen, weit herausgezoomt mind. 7/13/19 px),
Beschriftung = höchster Baum je Bestand, ab z14 einzelne Bäume ab 70 m. Punkte je Zoomstufe ausgedünnt (höchster je Zelle).
