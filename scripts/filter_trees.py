#!/usr/bin/env python3
"""Entfernt Baumspitzen, die in der OSM-Maske liegen (Gebäude, Türme, Masten, Strommasten, Windräder, ...).
usage: filter_trees.py <trees.csv> <mask.gpkg> <out.csv> <removed.csv>"""
import csv
import sys

from osgeo import ogr, osr

ogr.UseExceptions()
src, maskp, out, rem = sys.argv[1:5]
ds = ogr.Open(maskp)
layers = [ds.GetLayer(i) for i in range(ds.GetLayerCount())]
s = osr.SpatialReference(); s.ImportFromEPSG(4326); s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
t = layers[0].GetSpatialRef(); t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
tr = osr.CoordinateTransformation(s, t)
rows = list(csv.DictReader(open(src)))
keep, drop = [], []
for r in rows:
    x, y, _ = tr.TransformPoint(float(r["lon"]), float(r["lat"]))
    p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(x, y)
    hit = ""
    for lyr in layers:
        lyr.SetSpatialFilter(p)
        for f in lyr:
            if f.GetGeometryRef() and f.GetGeometryRef().Intersects(p):
                hit = lyr.GetName(); break
        if hit:
            break
    (drop if hit else keep).append(dict(r, osm=hit))
for path, rr in ((out, keep), (rem, drop)):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["osm"]); w.writeheader(); w.writerows(rr)
print(f"{len(rows)} Bäume, {len(drop)} in OSM-Maske entfernt (z.B. {[ (d['height_m'], d['osm']) for d in drop[:5]]})")
