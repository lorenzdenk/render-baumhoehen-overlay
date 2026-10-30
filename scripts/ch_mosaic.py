#!/usr/bin/env python3
"""Fügt die Jahrgänge des Vegetationshöhenmodells (je Jahr ein Ausschnitt, gleiches Raster) zusammen:
je Pixel der neueste Jahrgang mit gültigen Daten. Zusätzlich ein Byte-Raster mit dem Jahr (Jahr - 2000).
usage: ch_mosaic.py <out.tif> <out_year.tif> <jahr>:<datei> [...]   (Reihenfolge egal)"""
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
out, out_year, items = sys.argv[1], sys.argv[2], sorted(sys.argv[3:], reverse=True)   # neueste zuerst
ref = gdal.Open(items[0].split(":", 1)[1])
W, H = ref.RasterXSize, ref.RasterYSize
drv = gdal.GetDriverByName("GTiff")
o = drv.Create(out, W, H, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=YES"])
oy = drv.Create(out_year, W, H, 1, gdal.GDT_Byte, ["COMPRESS=DEFLATE", "TILED=YES"])
for d in (o, oy):
    d.SetGeoTransform(ref.GetGeoTransform()); d.SetProjection(ref.GetProjection())
o.GetRasterBand(1).SetNoDataValue(-9999); oy.GetRasterBand(1).SetNoDataValue(0)
dss = [(int(i.split(":", 1)[0]), gdal.Open(i.split(":", 1)[1])) for i in items]
B = 4096
cover = {y: 0 for y, _ in dss}
for r0 in range(0, H, B):
    rows = min(B, H - r0)
    res = np.full((rows, W), -9999, np.float32)
    yr = np.zeros((rows, W), np.uint8)
    for y, ds in dss:
        band = ds.GetRasterBand(1)
        a = band.ReadAsArray(0, r0, W, rows).astype(np.float32)
        nd = band.GetNoDataValue()
        valid = np.isfinite(a) & (a > -50) & (a < 300)
        if nd is not None:
            valid &= a != nd
        if band.DataType == gdal.GDT_Byte:   # Byte-Zwischenformat: Höhe * 2
            a = a * 0.5
        take = valid & (yr == 0)
        res[take] = a[take]; yr[take] = y - 2000
        cover[y] += int(take.sum())
    o.GetRasterBand(1).WriteArray(res, 0, r0); oy.GetRasterBand(1).WriteArray(yr, 0, r0)
o = oy = None
tot = W * H
print("Anteil je Jahrgang (neuester gültiger):", {y: f"{c / tot * 100:.1f} %" for y, c in sorted(cover.items())},
      f"ohne Daten: {(tot - sum(cover.values())) / tot * 100:.1f} %")
