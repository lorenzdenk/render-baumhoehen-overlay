#!/usr/bin/env python3
"""Kronenhöhenmodell (z.B. theLIST Tree Canopy Height) einmalig aufbereiten:

1. Kacheln (Standard 20 x 20 km, Originalprojektion) als Byte-GeoTIFF: Wert = Höhe * 2 (0,5-m-Schritte, 0..127 m),
   255 = keine Daten. Leere Kacheln (nur keine Daten) werden weggelassen. Dazu index.csv (Datei, Ausdehnung, EPSG).
2. Liste der Baumspitzen ab --min-tree Metern (Standard 50): lokale Maxima mit Plausibilitätsprüfung.

usage: chm_archive.py <src raster/vrt> <out dir> [--tile-km 20] [--min-tree 50] [--threads 4]

Baumspitze = Pixel >= min-tree, höchster Wert im Umkreis von 5 m (Plateaus: erstes Pixel).
Verworfen (Datei rejects.csv mit Grund):
  - spike:  weniger als 4 Pixel im Umkreis von 6 m mit Höhe >= 70 % der Spitze (keine Krone, z.B. Vogel, Leitung, Mast)
  - open:   im Umkreis von 30 m weniger als 25 % Pixel >= 15 m (steht nicht im Wald: Mast, Turm, Gebäude)
  - high:   über 110 m (höher als jeder bekannte Baum: Messfehler)
"""
import argparse
import csv
import math
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
from osgeo import gdal, osr
from scipy import ndimage

gdal.UseExceptions()


def disk(r):
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def work(args):
    src, out, i, j, x0, y1, size_px, res, min_tree, epsg = args
    ds = gdal.Open(src)
    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)
    nod = band.GetNoDataValue()
    margin = int(math.ceil(30 / res)) + 2
    # Fenster in Quellpixeln (mit Rand für die Nachbarschaftsprüfung)
    c0 = int(round((x0 - gt[0]) / gt[1])) - margin
    r0 = int(round((gt[3] - y1) / -gt[5])) - margin
    n = size_px + 2 * margin
    cc0, rr0 = max(c0, 0), max(r0, 0)
    cc1, rr1 = min(c0 + n, ds.RasterXSize), min(r0 + n, ds.RasterYSize)
    if cc1 <= cc0 or rr1 <= rr0:
        return i, j, None, [], []
    a = np.full((n, n), np.nan, np.float32)
    a[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = band.ReadAsArray(cc0, rr0, cc1 - cc0, rr1 - rr0).astype(np.float32)
    if nod is not None:
        a[a == nod] = np.nan
    a[(a < -50) | (a > 1000)] = np.nan
    core = a[margin:margin + size_px, margin:margin + size_px]
    valid = np.isfinite(core)
    if not valid.any():
        return i, j, None, [], []

    # 1. Byte-Kachel
    b = np.where(valid, np.clip(np.rint(np.nan_to_num(core) * 2), 0, 254), 255).astype(np.uint8)
    name = f"chm_{int(x0 // 1000)}_{int((y1 - size_px * res) // 1000)}.tif"
    path = os.path.join(out, "tiles", name)
    d = gdal.GetDriverByName("GTiff").Create(path, size_px, size_px, 1, gdal.GDT_Byte,
                                             ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "ZLEVEL=9"])
    d.SetGeoTransform((x0, res, 0, y1, 0, -res))
    sr = osr.SpatialReference(); sr.ImportFromEPSG(epsg); d.SetSpatialRef(sr)
    d.GetRasterBand(1).SetNoDataValue(255)
    d.GetRasterBand(1).WriteArray(b)
    d = None

    # 2. Baumspitzen
    trees, rejects = [], []
    h = np.nan_to_num(a, nan=0.0)
    cand = h >= min_tree
    cand[:margin, :] = cand[-margin:, :] = False
    cand[:, :margin] = cand[:, -margin:] = False
    if cand.any():
        r5 = max(int(round(5 / res)), 1)
        mx = ndimage.maximum_filter(h, footprint=disk(r5), mode="constant")
        peaks = cand & (h >= mx)
        # Plateaus: nur ein Pixel je zusammenhängendem Gipfel
        lab, nl = ndimage.label(peaks)
        idx = ndimage.find_objects(lab)
        r6 = max(int(round(6 / res)), 1)
        r30 = int(round(30 / res))
        d6, d30 = disk(r6), disk(r30)
        for k, sl in enumerate(idx, 1):
            ys, xs = np.nonzero(lab[sl] == k)
            py, px = ys[0] + sl[0].start, xs[0] + sl[1].start
            top = float(h[py, px])
            w6 = h[py - r6:py + r6 + 1, px - r6:px + r6 + 1]
            crown = int(((w6 >= 0.7 * top) & d6).sum())
            w30 = a[py - r30:py + r30 + 1, px - r30:px + r30 + 1]
            m30 = d30 & np.isfinite(w30)
            forest = float(((np.nan_to_num(w30) >= 15) & m30).sum() / max(m30.sum(), 1))
            X = x0 + (px - margin + 0.5) * res
            Y = y1 - (py - margin + 0.5) * res
            rec = (X, Y, round(top, 1), crown, round(forest, 2))
            if top > 110:
                rejects.append(rec + ("high",))
            elif crown < 4:
                rejects.append(rec + ("spike",))
            elif forest < 0.25:
                rejects.append(rec + ("open",))
            else:
                trees.append(rec)
    return i, j, (name, x0, y1 - size_px * res, x0 + size_px * res, y1), trees, rejects


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--tile-km", type=float, default=20)
    ap.add_argument("--min-tree", type=float, default=50)
    ap.add_argument("--threads", type=int, default=os.cpu_count())
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "tiles"), exist_ok=True)
    ds = gdal.Open(a.src)
    gt = ds.GetGeoTransform()
    res = gt[1]
    sr = ds.GetSpatialRef()
    sr.AutoIdentifyEPSG()
    epsg = int(sr.GetAuthorityCode(None))
    print(f"Quelle {ds.RasterXSize} x {ds.RasterYSize} px, {res} m, EPSG:{epsg}, "
          f"Typ {gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)}, NoData {ds.GetRasterBand(1).GetNoDataValue()}", flush=True)
    tile = a.tile_km * 1000
    size_px = int(round(tile / res))
    xmin, ymax = gt[0], gt[3]
    xmax, ymin = xmin + ds.RasterXSize * res, ymax + ds.RasterYSize * gt[5]
    gx0 = math.floor(xmin / tile) * tile
    gy1 = math.ceil(ymax / tile) * tile
    jobs = []
    j = 0
    y = gy1
    while y > ymin:
        i = 0; x = gx0
        while x < xmax:
            jobs.append((a.src, a.out, i, j, x, y, size_px, res, a.min_tree, epsg))
            x += tile; i += 1
        y -= tile; j += 1
    ds = None
    print(f"{len(jobs)} Kacheln à {a.tile_km:g} km", flush=True)
    t0 = time.time()
    index, trees, rejects = [], [], []
    with Pool(a.threads) as pool:
        for k, (i, j, meta, tr, rj) in enumerate(pool.imap_unordered(work, jobs), 1):
            if meta:
                index.append(meta)
            trees += tr; rejects += rj
            if k % 10 == 0 or k == len(jobs):
                print(f"  {k}/{len(jobs)} Kacheln, {len(index)} mit Daten, {len(trees)} Bäume, {len(rejects)} verworfen, "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)
    s = osr.SpatialReference(); s.ImportFromEPSG(epsg)
    t = osr.SpatialReference(); t.ImportFromEPSG(4326); t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to4326 = osr.CoordinateTransformation(s, t)
    with open(os.path.join(a.out, "index.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["file", "minx", "miny", "maxx", "maxy", "epsg"])
        for r in sorted(index):
            w.writerow(list(r) + [epsg])
    for name, rows, extra in (("trees.csv", trees, []), ("rejects.csv", rejects, ["reason"])):
        rows.sort(key=lambda r: -r[2])
        with open(os.path.join(a.out, name), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["lon", "lat", "height_m", "crown_px", "forest_frac", "x", "y"] + extra)
            for r in rows:
                lon, lat, _ = to4326.TransformPoint(r[0], r[1])
                w.writerow([f"{lon:.6f}", f"{lat:.6f}", r[2], r[3], r[4], f"{r[0]:.1f}", f"{r[1]:.1f}"] + list(r[5:]))
    hs = np.array([r[2] for r in trees]) if trees else np.zeros(0)
    print(f"fertig: {len(index)} Kacheln, {len(trees)} Bäume >= {a.min_tree:g} m "
          f"(>= 60: {(hs >= 60).sum()}, >= 70: {(hs >= 70).sum()}, >= 80: {(hs >= 80).sum()}, >= 90: {(hs >= 90).sum()}), "
          f"höchster {hs.max() if len(hs) else 0} m; verworfen {len(rejects)}; {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
