#!/usr/bin/env python3
"""Float-Höhenraster -> Byte (Höhe*2, 0..127 m, 255 = keine Daten), blockweise.
usage: ch_tobyte.py <in.tif> <out.tif>"""
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
src = gdal.Open(sys.argv[1]); b = src.GetRasterBand(1); nd = b.GetNoDataValue()
o = gdal.GetDriverByName("GTiff").Create(sys.argv[2], src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Byte,
                                         ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "BIGTIFF=YES"])
o.SetGeoTransform(src.GetGeoTransform()); o.SetProjection(src.GetProjection())
o.GetRasterBand(1).SetNoDataValue(255)
valid_px = 0
for r0 in range(0, src.RasterYSize, 4096):
    rows = min(4096, src.RasterYSize - r0)
    a = b.ReadAsArray(0, r0, src.RasterXSize, rows).astype(np.float32)
    v = np.isfinite(a) & (a > -50) & (a < 300)
    if nd is not None:
        v &= a != nd
    valid_px += int(v.sum())
    o.GetRasterBand(1).WriteArray(np.where(v, np.clip(np.rint(a * 2), 0, 254), 255).astype(np.uint8), 0, r0)
o = None
print(f"{sys.argv[2]}: {valid_px / (src.RasterXSize * src.RasterYSize) * 100:.1f} % mit Daten")
