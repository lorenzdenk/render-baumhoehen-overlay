#!/usr/bin/env python3
"""Entfernt Baumspitzen, die in der OSM-Maske liegen (Gebäude, Türme, Masten, Strommasten, Windräder, Leitungen).
Schnell auch für Millionen Punkte: die Maske wird je 20-km-Kachel mit 2 m gerastert und nachgeschlagen.
usage: filter_trees.py <trees.csv> <mask.gpkg> <out.csv> <removed.csv>"""
import csv
import sys
from collections import defaultdict

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
src, maskp, out, rem = sys.argv[1:5]
ds = ogr.Open(maskp)
layers = [ds.GetLayer(i) for i in range(ds.GetLayerCount())]
s = osr.SpatialReference(); s.ImportFromEPSG(4326); s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
t = layers[0].GetSpatialRef(); t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
tr = osr.CoordinateTransformation(s, t)
rows = list(csv.DictReader(open(src)))
pts = np.array(tr.TransformPoints([(float(r["lon"]), float(r["lat"])) for r in rows]))[:, :2]
T, RES = 20000.0, 2.0
n = int(T / RES)
tiles = defaultdict(list)
for i, (x, y) in enumerate(pts):
    tiles[(int(x // T), int(y // T))].append(i)
hit = np.zeros(len(rows), bool)
for k, ((tx, ty), ids) in enumerate(tiles.items(), 1):
    x0, y1 = tx * T, (ty + 1) * T
    mem = gdal.GetDriverByName("MEM").Create("", n, n, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((x0, RES, 0, y1, 0, -RES))
    mem.SetSpatialRef(t)
    for lyr in layers:
        lyr.SetSpatialFilterRect(x0, y1 - T, x0 + T, y1)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    m = mem.GetRasterBand(1).ReadAsArray()
    ids = np.array(ids)
    c = np.clip(((pts[ids, 0] - x0) / RES).astype(int), 0, n - 1)
    r = np.clip(((y1 - pts[ids, 1]) / RES).astype(int), 0, n - 1)
    hit[ids] = m[r, c] > 0
    if k % 20 == 0:
        print(f"  {k}/{len(tiles)} Kacheln, {hit.sum()} entfernt", flush=True)
for path, sel in ((out, ~hit), (rem, hit)):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader()
        w.writerows(r for r, s_ in zip(rows, sel) if s_)
hs = np.array([float(r["height_m"]) for r in rows])
print(f"{len(rows)} Bäume, {hit.sum()} in OSM-Maske entfernt (davon >= 80 m: {(hit & (hs >= 80)).sum()}, "
      f">= 100 m: {(hit & (hs >= 100)).sum()})")
