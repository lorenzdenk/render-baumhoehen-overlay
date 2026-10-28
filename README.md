# Baumhöhen-Overlay

Baumhöhe (Kronenhöhe über Grund) als **MBTiles-Overlay** für LoriMaps: 512-px-Kacheln, WebP mit Transparenz,
identische Kacheln nur einmal gespeichert (gleiches Schema wie `lorimaps-render`).

> **GitHub:** Ein Push auf `render.env`, `regions/` oder die Skripte startet den Workflow „Render Baumhöhen-Overlay“
> (oder manuell mit Eingabe der Region). Ergebnis unter *Releases* (`baumhoehe-<NAME>`), Logs und Vorschaubilder
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
Pro Block: nDOM laden → nach Web Mercator (0,8 m bei z16) → Maske → 2-Band-Zwischenraster (Höhe×4, Kronendeckung)
mit gemittelten Übersichten → Kacheln z11–z16 → Block löschen. Damit bleibt der Platzbedarf pro Rechner klein, egal wie
gross das Gebiet ist. Ein Abschlussjob rendert z8–z10 aus den Block-Übersichten und fügt alles zusammen; wird die Datei
grösser als 1,9 GB (Release-Grenze), bleiben es Teile `…-partKofN.mbtiles`.

Darstellung: Farbe = mittlere Kronenhöhe, Deckkraft = Kronendeckung (max. 80 %). Farbskala in `STOPS`
(gelb → türkis → blau → violett, bewusst nicht grün, damit sie sich vom Wald der Grundkarte abhebt).

## Regionen

`regions/konstanz.env` (Test, 4 Blöcke), `regions/bw.env` (ganz BW, 20 Shards). Region in `render.env` wählen.

## Lizenz der Ergebnisse

Baumhöhe: Datenquelle LGL, www.lgl-bw.de (nDOM1), dl-de/by-2-0, Daten geändert. Maske: © OpenStreetMap-Mitwirkende (ODbL).
Steht in den Metadaten jeder MBTiles-Datei.
