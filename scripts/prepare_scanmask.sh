#!/usr/bin/env bash
# Maske für die Baumsuche (EPSG:25832): Flächen und Leitungen wie prepare_mask.sh, dazu Punkte (Türme, Masten,
# Schornsteine, Strommasten, Windräder) mit 20 m Radius.
# usage: prepare_scanmask.sh <in.osm.pbf> <out.gpkg> [EPSG, Standard 25832]
set -euo pipefail
in="$1"; out="$2"; epsg="${3:-25832}"; tmp=$(mktemp -d)
osmium tags-filter "$in" \
  a/building a/building:part a/natural=water a/waterway=riverbank,dock,canal a/landuse=reservoir,basin \
  a/man_made=bridge,storage_tank,silo,tower,wastewater_plant,mast,chimney a/amenity=parking a/power=plant,substation \
  -o "$tmp/areas.osm.pbf" --overwrite
osmium tags-filter "$in" w/bridge w/power=minor_line w/aerialway w/man_made=pipeline -o "$tmp/lines.osm.pbf" --overwrite
osmium tags-filter "$in" w/power=line -o "$tmp/power.osm.pbf" --overwrite
osmium tags-filter "$in" n/man_made=tower,mast,chimney,communications_tower,water_tower,lighthouse,crane \
  n/power=tower,pole,portal,generator n/aerialway=pylon n/generator:source=wind n/tower:type \
  -o "$tmp/points.osm.pbf" --overwrite
rm -f "$out"
ogr2ogr -f GPKG "$out" "$tmp/areas.osm.pbf" multipolygons -t_srs EPSG:$epsg -nln areas -select osm_id -nlt PROMOTE_TO_MULTI
ogr2ogr -f GPKG "$tmp/l.gpkg" "$tmp/lines.osm.pbf" lines -t_srs EPSG:$epsg -nln lines -select osm_id
ogr2ogr -f GPKG -update "$out" "$tmp/l.gpkg" -nln lines_buf -dialect SQLite -sql "SELECT ST_Buffer(geom, 5) AS geom FROM lines" -nlt POLYGON
ogr2ogr -f GPKG "$tmp/p.gpkg" "$tmp/points.osm.pbf" points -t_srs EPSG:$epsg -nln points -select osm_id
ogr2ogr -f GPKG -update "$out" "$tmp/p.gpkg" -nln points_buf -dialect SQLite -sql "SELECT ST_Buffer(geom, 20) AS geom FROM points" -nlt POLYGON
ogr2ogr -f GPKG "$tmp/pw.gpkg" "$tmp/power.osm.pbf" lines -t_srs EPSG:$epsg -nln power -select osm_id
ogr2ogr -f GPKG -update "$out" "$tmp/pw.gpkg" -nln power_buf -dialect SQLite -sql "SELECT ST_Buffer(geom, 15) AS geom FROM power" -nlt POLYGON
for l in areas lines_buf points_buf power_buf; do ogrinfo -so "$out" $l | grep 'Feature Count' | sed "s/^/$l /"; done
rm -rf "$tmp"
