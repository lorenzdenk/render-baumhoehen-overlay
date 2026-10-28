#!/usr/bin/env bash
# Maske aus OSM: Gebäude, Wasser, Brücken usw. -> mask.gpkg (EPSG:3857). Im nDOM sind das keine Bäume.
# usage: prepare_mask.sh <in.osm.pbf> <out.gpkg>
set -euo pipefail
in="$1"; out="$2"; tmp=$(mktemp -d)
osmium tags-filter "$in" \
  a/building a/building:part a/natural=water a/waterway=riverbank,dock,canal a/landuse=reservoir,basin \
  a/man_made=bridge,storage_tank,silo,tower,wastewater_plant a/amenity=parking \
  -o "$tmp/areas.osm.pbf" --overwrite
osmium tags-filter "$in" w/bridge w/power=minor_line w/aerialway -o "$tmp/lines.osm.pbf" --overwrite
osmium tags-filter "$in" w/power=line -o "$tmp/power.osm.pbf" --overwrite
rm -f "$out"
ogr2ogr -f GPKG "$out" "$tmp/areas.osm.pbf" multipolygons -t_srs EPSG:3857 -nln areas -select osm_id -nlt PROMOTE_TO_MULTI
ogr2ogr -f GPKG "$tmp/lines.gpkg" "$tmp/lines.osm.pbf" lines -t_srs EPSG:3857 -nln lines -select osm_id
# Brücken, Strom- und Seilbahnleitungen: 8 m breiter Streifen
ogr2ogr -f GPKG -update "$out" "$tmp/lines.gpkg" -nln lines_buf -dialect SQLite \
  -sql "SELECT ST_Buffer(geom, 4) AS geom FROM lines" -nlt POLYGON
# Hochspannungsleitungen: Seile hängen seitlich weit aus -> 15 m Radius
ogr2ogr -f GPKG "$tmp/pw.gpkg" "$tmp/power.osm.pbf" lines -t_srs EPSG:3857 -nln power -select osm_id
ogr2ogr -f GPKG -update "$out" "$tmp/pw.gpkg" -nln power_buf -dialect SQLite -sql "SELECT ST_Buffer(geom, 15) AS geom FROM power" -nlt POLYGON
ogrinfo -so "$out" areas | grep -E 'Feature Count' || true
ogrinfo -so "$out" lines_buf | grep -E 'Feature Count' || true
rm -rf "$tmp"
