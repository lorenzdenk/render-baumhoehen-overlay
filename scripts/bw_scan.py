#!/usr/bin/env python3
"""Suche nach den höchsten Bäumen in Baden-Württemberg.

Stufe 1 (nDOM1, alle 2-km-Einheiten des Shards): Baumspitzen ab --min (Standard 40 m) wie in chm_archive.py
  (lokales Maximum im Umkreis 5 m, Krone >= 4 px im Umkreis 6 m mit >= 70 % der Höhe, Wald im Umkreis 30 m),
  ohne Spitzen in der OSM-Maske (Gebäude, Türme, Masten, Schornsteine, Strommasten, Windräder, Brücken, Leitungen).
Stufe 2 (nur Kandidaten ab --remeasure, Standard 48 m): Nachmessung mit DOM1 (Laserscan, höchste Punkte) minus
  DGM1 (Laserscan, Boden): höchster DOM-Wert im Umkreis 5 m minus Boden an dieser Stelle. Das nDOM ist laut LGL
  auf -1..55 m begrenzt und stammt aus Bildern; das Laser-DOM misst die Spitze genauer.

usage: bw_scan.py --shard K --shards N --mask mask25832.gpkg --out cand_K.csv
"""
import argparse
import csv
import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from osgeo import gdal, ogr, osr
from scipy import ndimage

gdal.UseExceptions()
BASE = "https://opengeodata.lgl-bw.de/data"
UA = {"User-Agent": "render-baumhoehen-overlay (+https://github.com/lorenzdenk/render-baumhoehen-overlay)"}
# Ausdehnung BW in UTM32 (km), Einheiten: Ost ungerade, Nord gerade
E0, E1, N0, N1 = 387, 611, 5262, 5516


def disk(r):
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


D5, D6, D30 = disk(5), disk(6), disk(30)


def fetch(url):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
            time.sleep(4 * (attempt + 1))
        except Exception:
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Download fehlgeschlagen: {url}")


def unzip_rasters(data, d, exts):
    os.makedirs(d, exist_ok=True)
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():
            if m.lower().endswith(exts):
                p = os.path.join(d, os.path.basename(m))
                with open(p, "wb") as f:
                    f.write(z.read(m))
                out.append(p)
    return sorted(out)


def mosaic(files, e, n):
    """2x2 km Mosaik (2000 x 2000, 1 m) im Raster der Einheit, NaN = keine Daten."""
    vrt = gdal.BuildVRT("", files)
    w = gdal.Warp("", vrt, format="MEM", outputBounds=(e * 1000, n * 1000, e * 1000 + 2000, n * 1000 + 2000),
                  xRes=1, yRes=1, resampleAlg="near", dstNodata=-9999, outputType=gdal.GDT_Float32,
                  dstSRS="EPSG:25832")
    a = w.GetRasterBand(1).ReadAsArray()
    a[(a < -100) | (a > 2000)] = np.nan
    return a


def mask_for(maskds, e, n):
    mem = gdal.GetDriverByName("MEM").Create("", 2000, 2000, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((e * 1000, 1, 0, n * 1000 + 2000, 0, -1))
    mem.SetProjection("EPSG:25832")
    for i in range(maskds.GetLayerCount()):
        lyr = maskds.GetLayer(i)
        lyr.SetSpatialFilterRect(e * 1000, n * 1000, e * 1000 + 2000, n * 1000 + 2000)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    return mem.GetRasterBand(1).ReadAsArray().astype(bool)


def peaks(h, hmin):
    hz = np.nan_to_num(h, nan=0.0)
    cand = hz >= hmin
    if not cand.any():
        return []
    mx = ndimage.maximum_filter(hz, footprint=D5, mode="constant")
    hp = np.pad(h, 30, constant_values=np.nan)
    pk = cand & (hz >= mx)
    lab, nl = ndimage.label(pk)
    res = []
    for k, sl in enumerate(ndimage.find_objects(lab), 1):
        ys, xs = np.nonzero(lab[sl] == k)
        py, px = ys[0] + sl[0].start, xs[0] + sl[1].start
        if py < 6 or px < 6 or py >= h.shape[0] - 6 or px >= h.shape[1] - 6:
            continue
        top = float(hz[py, px])
        crown = int(((hz[py - 6:py + 7, px - 6:px + 7] >= 0.7 * top) & D6).sum())
        w30 = hp[py:py + 61, px:px + 61]  # hp ist um 30 px mit NaN erweitert
        dd = D30
        m = dd & np.isfinite(w30)
        forest = float(((np.nan_to_num(w30) >= 15) & m).sum() / max(m.sum(), 1))
        res.append((py, px, top, crown, forest))
    return res


def unit(args):
    try:
        return unit_(args)
    except Exception as ex:   # eine fehlerhafte Einheit darf den Lauf nicht abbrechen
        print(f"    FEHLER Einheit {args[0]}_{args[1]}: {ex!r}", flush=True)
        return args[0], args[1], None, []


def unit_(args):
    e, n, maskpath, hmin, hre, work = args
    data = fetch(f"{BASE}/ndom1/ndom1_32_{e}_{n}_2_bw.zip")
    if data is None:
        return e, n, None, []
    d = os.path.join(work, f"{e}_{n}")
    files = unzip_rasters(data, d + "_n", (".tif",))
    if not files:
        return e, n, None, []
    h = mosaic(files, e, n)
    stats = (int(np.isfinite(h).sum()), float(np.nanmax(h)) if np.isfinite(h).any() else 0.0)
    out = []
    pk = peaks(h, hmin)
    if pk:
        md = ogr.Open(maskpath)
        mask = mask_for(md, e, n)
        md = None
        pk = [p for p in pk if not mask[p[0], p[1]]]
    good = [p for p in pk if p[3] >= 4 and p[4] >= 0.25]
    remeasure = [p for p in good if p[2] >= hre]
    lid = {}
    if remeasure:
        dom = fetch(f"{BASE}/dom1/dom1_32_{e}_{n}_2_bw.zip")
        dgm = fetch(f"{BASE}/dgm/dgm1_32_{e}_{n}_2_bw.zip")
        if dom and dgm:
            fd = unzip_rasters(dom, d + "_dom", (".tif",))
            fg = unzip_rasters(dgm, d + "_dgm", (".xyz", ".tif"))
        if dom and dgm and fd and fg:
            years = sorted({f.rsplit("_", 1)[-1].split(".")[0] for f in fd + fg if f.rsplit("_", 1)[-1][:2] in ("19", "20")})
            dsm = np.pad(mosaic(fd, e, n), 8, constant_values=np.nan)   # Rand, damit Fenster nie abgeschnitten werden
            dtm = np.pad(mosaic(fg, e, n), 8, constant_values=np.nan)
            for py0, px0, top, crown, forest in remeasure:
                py, px = py0 + 8, px0 + 8
                w = dsm[py - 5:py + 6, px - 5:px + 6]
                wz = np.where(D5, np.nan_to_num(w, nan=-1e9), -1e9)
                k = np.unravel_index(np.argmax(wz), wz.shape)
                qy, qx = py - 5 + k[0], px - 5 + k[1]
                g = dtm[qy - 2:qy + 3, qx - 2:qx + 3]
                if np.isfinite(dsm[qy, qx]) and np.isfinite(g).any():
                    ground = float(np.nanmin(g))
                    top_l = float(dsm[qy, qx]) - ground
                    # Krone im Laser-DOM: Pixel im Umkreis 3 m, die höchstens 6 m unter der Spitze liegen
                    wl = dsm[qy - 3:qy + 4, qx - 3:qx + 4] - ground
                    lc = int(((np.nan_to_num(wl, nan=-1e9) >= top_l - 6) & disk(3)).sum())
                    lid[(py0, px0)] = (round(top_l, 2), qy - 8, qx - 8, round(float(dsm[qy, qx]), 2), "/".join(years), lc)
        for s in ("_dom", "_dgm"):
            for f in os.listdir(d + s) if os.path.isdir(d + s) else []:
                os.remove(os.path.join(d + s, f))
    for f in files:
        os.remove(f)
    for py, px, top, crown, forest in pk:
        reason = "" if (crown >= 4 and forest >= 0.25) else ("spike" if crown < 4 else "open")
        L = lid.get((py, px))
        X, Y = e * 1000 + px + 0.5, n * 1000 + 2000 - py - 0.5
        if L:
            X, Y = e * 1000 + L[2] + 0.5, n * 1000 + 2000 - L[1] - 0.5
        if L and not reason and (L[5] < 3 or L[0] - top > 15):
            reason = "laser-spike"   # einzelner Ausreisser im Laser-DOM (Vogel, Leitung, Kran)
        out.append((X, Y, round(top, 2), crown, round(forest, 2), L[0] if L else "", L[5] if L else "",
                    L[3] if L else "", L[4] if L else "", reason))
    return e, n, stats, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0); ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--mask", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--min", type=float, default=45); ap.add_argument("--remeasure", type=float, default=48)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--units", default=None, help="nur diese Einheiten (E_N,E_N,...) zum Testen")
    a = ap.parse_args()
    units = [(e, n) for n in range(N0, N1 + 1, 2) for e in range(E0, E1 + 1, 2)]
    if a.units:
        units = [tuple(map(int, u.split("_"))) for u in a.units.split(",")]
    mine = units[a.shard::a.shards]
    work = os.path.abspath("scanwork"); os.makedirs(work, exist_ok=True)
    print(f"Shard {a.shard}/{a.shards}: {len(mine)} Einheiten", flush=True)
    t0 = time.time()
    found = n_data = 0
    hist = np.zeros(130, int)
    s = osr.SpatialReference(); s.ImportFromEPSG(25832); s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    t = osr.SpatialReference(); t.ImportFromEPSG(4326); t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(s, t)
    with open(a.out, "w", newline="") as f, ThreadPoolExecutor(a.threads) as ex:
        w = csv.writer(f)
        w.writerow(["lon", "lat", "ndom_m", "crown_px", "forest_frac", "lidar_m", "lidar_crown_px", "dom_nhn_m", "jahre", "verworfen",
                    "x", "y"])
        for k, (e, n, stats, rows) in enumerate(ex.map(unit, [(e, n, a.mask, a.min, a.remeasure, work) for e, n in mine]), 1):
            if stats:
                n_data += 1
                hist[min(int(stats[1]), 129)] += 1
            for r in rows:
                lon, lat, _ = tr.TransformPoint(r[0], r[1])
                w.writerow([f"{lon:.6f}", f"{lat:.6f}"] + list(r[2:]) + [f"{r[0]:.1f}", f"{r[1]:.1f}"])
                found += 1
            if k % 25 == 0 or k == len(mine):
                f.flush()
                print(f"  {k}/{len(mine)} Einheiten, {n_data} mit Daten, {found} Spitzen, "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)
    print("Maximum je Einheit (m: Anzahl):", {i: int(c) for i, c in enumerate(hist) if c and i >= 40}, flush=True)


if __name__ == "__main__":
    main()
