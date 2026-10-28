#!/usr/bin/env python3
"""Baumhöhen-Overlay aus dem nDOM1 des LGL Baden-Württemberg als MBTiles (512-px-WebP mit Transparenz).

Ablauf (skaliert bis ganz BW):
  Das Gebiet wird in Kacheln der Zoomstufe ZSPLIT (Standard 11, ca. 13 x 13 km) zerlegt. Jede dieser
  "Blockkacheln" wird für sich verarbeitet: nDOM-Kacheln laden, nach Web Mercator umrechnen, Gebäude und
  Wasser (OSM) ausmaskieren, Zoomstufen ZSPLIT..MAXZOOM rendern, Übersicht für die kleinen Zoomstufen
  speichern, aufräumen. Die Blockkacheln werden auf mehrere Rechner (Shards) verteilt.
  Danach rendert "overview" die Zoomstufen MINZOOM..ZSPLIT-1 aus den gespeicherten Übersichten.

Kodierung im Zwischenraster (3 Bänder, Byte):
  Band 1 = Baumhöhe * 2 (0,5-m-Schritte bis 127 m, 0 = keine Krone), Band 2 = 255 wo Krone (Höhe >= min_h), sonst 0,
  Band 3 = wie Band 1, aber beim Verkleinern wird das Maximum genommen (für hervorgehobene Riesenbäume).
  Band 1 und 2 werden gemittelt; mittlere Kronenhöhe = Band1 / Band2 * 255 / 2, Deckung = Band2 / 255.

Quellen: --source lgl (nDOM1-Kacheln des LGL, Standard) oder --source raster:<Datei> (beliebiges
Kronenhöhen-Raster in Metern, beliebige Projektion, z.B. theLIST Tree Canopy Height für Tasmanien).

Befehle:
  baumhoehe.py blocks  <W> <S> <E> <N> [--zsplit 11]               Blockkacheln des Gebiets auflisten
  baumhoehe.py build   --bbox W,S,E,N --shard K --shards N --out part.mbtiles --lowres DIR [--osm mask.gpkg]
  baumhoehe.py overview --lowres DIR --bbox ... --out overview.mbtiles
  baumhoehe.py legend  legend.png
"""
import argparse
import csv
import hashlib
import io
import math
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool

import numpy as np
from osgeo import gdal, ogr, osr
from PIL import Image

gdal.UseExceptions()
R = 20037508.342789244
TILE = 512
LGL = "https://opengeodata.lgl-bw.de/data/ndom1/ndom1_32_{e}_{n}_2_bw.zip"
UA = {"User-Agent": "render-baumhoehen-overlay (+https://github.com/lorenzdenk/render-baumhoehen-overlay)"}

HS = 2  # Höhe * HS = Bandwert (0,5-m-Schritte, bis 127 m)

# Farbskalen: (Höhe m, (R, G, B), Deckkraft). Bewusst nicht grün, damit sie sich vom Wald der Grundkarte abhebt.
PALETTES = {
    # Baden-Württemberg: ganze Bandbreite 2-45 m
    "standard": dict(min_h=2.0, highlight=None, ticks=(2, 5, 10, 15, 20, 25, 30, 35, 40, 45), legend_max=45, stops=[
        (2, (255, 255, 204), .8), (5, (199, 233, 180), .8), (10, (127, 205, 187), .8), (15, (65, 182, 196), .8),
        (20, (29, 145, 192), .8), (25, (34, 94, 168), .8), (30, (37, 52, 148), .8), (35, (8, 29, 88), .8),
        (45, (74, 20, 134), .8)]),
    # Riesenbäume: unter 50 m zurückhaltend grau-blau und halbtransparent, ab 50 m kräftig gelb -> rot -> violett.
    # Ab "highlight" werden Bäume auf allen Zoomstufen mit ihrer Maximalhöhe gezeichnet (verschwinden nicht im Mittel).
    "riesen": dict(min_h=5.0, highlight=50.0, ticks=(5, 20, 35, 50, 60, 70, 80, 90, 100), legend_max=100, stops=[
        (5, (215, 222, 226), .30), (35, (150, 170, 190), .45), (49.9, (105, 125, 160), .55),
        (50, (255, 225, 0), .95), (60, (255, 140, 0), .95), (70, (225, 25, 25), .95), (80, (200, 0, 150), 1.0),
        (90, (110, 0, 170), 1.0), (100, (40, 0, 90), 1.0)]),
}
P = PALETTES["standard"]
LUT = None
LUTA = None


def set_palette(name):
    """Farbtabelle (RGB und Deckkraft) für Bandwerte 0..255."""
    global P, LUT, LUTA
    P = PALETTES[name]
    hs = np.arange(256) / HS
    xs = [st[0] for st in P["stops"]]
    LUT = np.zeros((256, 3), np.uint8)
    for c in range(3):
        LUT[:, c] = np.interp(hs, xs, [st[1][c] for st in P["stops"]]).round().astype(np.uint8)
    LUTA = np.interp(hs, xs, [st[2] for st in P["stops"]]).astype(np.float32)


set_palette("standard")


# ---------------------------------------------------------------- Kachelrechnung
def tile_size(z):
    return 2 * R / 2 ** z


def tile_bounds(z, x, y):
    s = tile_size(z)
    return -R + x * s, R - (y + 1) * s, -R + (x + 1) * s, R - y * s  # minx, miny, maxx, maxy (3857)


def lonlat_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def blocks(bbox, zsplit):
    w, s, e, n = bbox
    x0, y0 = lonlat_tile(w, n, zsplit)
    x1, y1 = lonlat_tile(e, s, zsplit)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def transformer(src, dst):
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromEPSG(src); b.ImportFromEPSG(dst)
    a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    b.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(a, b)


T_3857_UTM = transformer(3857, 25832)


def lgl_units(b3857, margin=30.0):
    """2-km-Downloadeinheiten (Ost ungerade, Nord gerade km), die ein 3857-Rechteck abdecken."""
    minx, miny, maxx, maxy = b3857
    pts = []
    for i in range(21):
        f = i / 20
        for (x, y) in ((minx + f * (maxx - minx), miny), (minx + f * (maxx - minx), maxy),
                       (minx, miny + f * (maxy - miny)), (maxx, miny + f * (maxy - miny))):
            pts.append(T_3857_UTM.TransformPoint(x, y)[:2])
    es = [p[0] for p in pts]; ns = [p[1] for p in pts]
    e0, e1 = (min(es) - margin) / 1000, (max(es) + margin) / 1000
    n0, n1 = (min(ns) - margin) / 1000, (max(ns) + margin) / 1000
    ue0 = 2 * math.floor((e0 - 1) / 2) + 1
    un0 = 2 * math.floor(n0 / 2)
    return [(e, n) for e in range(ue0, int(math.floor(e1)) + 1, 2) for n in range(un0, int(math.floor(n1)) + 1, 2)]


# ---------------------------------------------------------------- Download
def download_unit(args):
    (e, n), cache = args
    d = os.path.join(cache, f"{e}_{n}")
    if os.path.isdir(d):
        return (e, n), sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".tif"))
    url = LGL.format(e=e, n=n)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                os.makedirs(d, exist_ok=True)  # leer = ausserhalb BW
                return (e, n), []
            time.sleep(3 * (attempt + 1))
        except Exception:
            time.sleep(3 * (attempt + 1))
    else:
        print(f"    FEHLER Download {url}", flush=True)
        return (e, n), None
    tmp = d + ".tmp"
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():
            if m.lower().endswith(".tif"):
                with open(os.path.join(tmp, os.path.basename(m)), "wb") as f:
                    f.write(z.read(m))
    os.replace(tmp, d)
    return (e, n), sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".tif"))


# ---------------------------------------------------------------- Archivkacheln (chm_archive.py)
_index = {}


def index_tiles(url, b3857, cache):
    """Lädt die Archivkacheln (index.csv neben den Dateien), die ein 3857-Rechteck berühren."""
    if url not in _index:
        os.makedirs(cache, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
            rows = list(csv.DictReader(io.StringIO(r.read().decode())))
        _index[url] = rows
    rows = _index[url]
    if not rows:
        return []
    epsg = int(rows[0]["epsg"])
    t = transformer(3857, epsg)
    minx, miny, maxx, maxy = b3857
    pts = [t.TransformPoint(minx + f * (maxx - minx), y)[:2] for f in np.linspace(0, 1, 11) for y in (miny, maxy)]
    pts += [t.TransformPoint(x, miny + f * (maxy - miny))[:2] for f in np.linspace(0, 1, 11) for x in (minx, maxx)]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    bx0, bx1, by0, by1 = min(xs) - 50, max(xs) + 50, min(ys) - 50, max(ys) + 50
    base = url.rsplit("/", 1)[0]
    need = [r["file"] for r in rows if float(r["minx"]) < bx1 and float(r["maxx"]) > bx0
            and float(r["miny"]) < by1 and float(r["maxy"]) > by0]
    out = []
    for f in need:
        p = os.path.join(cache, f)
        if not os.path.exists(p):
            for attempt in range(6):
                try:
                    with urllib.request.urlopen(urllib.request.Request(f"{base}/{f}", headers=UA), timeout=300) as r, \
                            open(p + ".tmp", "wb") as o:
                        shutil.copyfileobj(r, o)
                    os.replace(p + ".tmp", p)
                    break
                except Exception as ex:
                    print("    retry", f, ex, flush=True)
                    time.sleep(5 * (attempt + 1))
            else:
                raise RuntimeError(f"Download fehlgeschlagen: {f}")
        out.append(p)
    return out


# ---------------------------------------------------------------- Rendern
def colorize(b1, b2, b3=None):
    """b1, b2: gemittelte Bänder, b3: Maximum (float). Gibt RGBA uint8 zurück oder None, wenn leer."""
    cov = b2 / 255.0
    if not (cov > 0.004).any():
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        hv = np.where(b2 > 0, b1 / np.maximum(b2, 1e-6) * 255.0, 0)
    idx = np.clip(np.rint(hv), 0, 255).astype(np.uint8)
    rgba = np.empty(b1.shape + (4,), np.uint8)
    rgba[..., :3] = LUT[idx]
    rgba[..., 3] = np.clip(np.rint(cov * LUTA[idx] * 255), 0, 255).astype(np.uint8)
    if P["highlight"] and b3 is not None:
        big = b3 >= P["highlight"] * HS
        if big.any():
            i3 = np.clip(np.rint(b3[big]), 0, 255).astype(np.uint8)
            rgba[big, :3] = LUT[i3]
            rgba[big, 3] = np.rint(LUTA[i3] * 255).astype(np.uint8)
    return rgba


def encode(rgba, quality):
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=quality, method=4)
    return buf.getvalue()


_ds = None


def _render_job(args):
    """Liest ein Fenster aus dem 2-Band-Raster (mit Übersichten) und gibt (z, x, y, webp) zurück."""
    global _ds
    path, z, x, y, xoff, yoff, size, quality, palette = args
    if LUT is None or P is not PALETTES[palette]:
        set_palette(palette)
    if _ds is None or _ds.GetDescription() != path:
        _ds = gdal.Open(path)
    rd = lambda k, alg: _ds.GetRasterBand(k).ReadAsArray(xoff, yoff, size, size, TILE, TILE,
                                                         resample_alg=alg).astype(np.float32)
    b1 = rd(1, gdal.GRIORA_Average)
    b2 = rd(2, gdal.GRIORA_Average)
    # Band 3: Übersichten enthalten bereits das Maximum; das passende Niveau hat genau TILE Pixel
    b3 = rd(3, gdal.GRIORA_NearestNeighbour) if P["highlight"] else None
    rgba = colorize(b1, b2, b3)
    return z, x, y, (encode(rgba, quality) if rgba is not None else None)


class MBTiles:
    """Deduplizierte MBTiles (Tabellen map + images, View tiles), wie in lorimaps-render."""

    def __init__(self, path, meta):
        if os.path.exists(path):
            os.remove(path)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            CREATE TABLE metadata (name TEXT, value TEXT);
            CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
            CREATE TABLE images (tile_id TEXT, tile_data BLOB);
            CREATE UNIQUE INDEX images_id ON images (tile_id);
            CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
            CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
                map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
        """)
        self.db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
        self.n = self.bytes = 0

    def add(self, z, x, y, data):
        h = hashlib.md5(data).hexdigest()
        cur = self.db.execute("INSERT OR IGNORE INTO images VALUES (?, ?)", (h, data))
        if cur.rowcount:
            self.bytes += len(data)
        self.db.execute("INSERT OR REPLACE INTO map VALUES (?, ?, ?, ?)", (z, x, (1 << z) - 1 - y, h))
        self.n += 1

    def close(self):
        self.db.commit()
        self.db.close()


def metadata(a, minzoom, maxzoom):
    w, s, e, n = a.bbox
    return {"name": a.title, "format": "webp", "type": "overlay", "version": "1",
            "description": a.description, "attribution": a.attribution,
            "minzoom": str(minzoom), "maxzoom": str(maxzoom), "bounds": f"{w},{s},{e},{n}",
            "center": f"{(w + e) / 2},{(s + n) / 2},{min(maxzoom, 13)}", "tile_size": str(TILE), "scale": "2"}


# ---------------------------------------------------------------- Blockkachel verarbeiten
def build_block(a, bx, by, cache, work, mb, pool):
    z0 = a.zsplit
    b = tile_bounds(z0, bx, by)
    px = TILE * 2 ** (a.maxzoom - z0)              # Pixel pro Seite im Zwischenraster
    res = tile_size(z0) / px
    t0 = time.time()

    if a.source == "lgl":
        units = lgl_units(b)
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(download_unit, [(u, cache) for u in units]))
        if any(f is None for _, f in got):
            raise RuntimeError("Download fehlgeschlagen")
        tifs = [t for _, f in got for t in f]
        print(f"  Block {z0}/{bx}/{by}: {len(units)} Einheiten, {len(tifs)} nDOM-Kacheln, "
              f"Download {time.time() - t0:.0f} s", flush=True)
    elif a.source.startswith("index:"):
        tifs = index_tiles(a.source[6:], b, cache)
        print(f"  Block {z0}/{bx}/{by}: {len(tifs)} Archivkacheln, {time.time() - t0:.0f} s", flush=True)
    else:
        tifs = [a.source.split(":", 1)[1]]
        print(f"  Block {z0}/{bx}/{by}: Quelle {tifs[0]}", flush=True)
    if not tifs:
        return 0
    scale = 0.5 if a.source.startswith("index:") else 1.0   # Archiv: Byte = Höhe * 2

    vrt = os.path.join(work, "src.vrt")
    gdal.BuildVRT(vrt, tifs)
    src = gdal.Open(vrt)
    nodata = src.GetRasterBand(1).GetNoDataValue()

    # Maske (Gebäude, Wasser, Brücken) im Zielraster
    mask = None
    if a.osm:
        mem = gdal.GetDriverByName("MEM").Create("", px, px, 1, gdal.GDT_Byte)
        mem.SetGeoTransform((b[0], res, 0, b[3], 0, -res))
        mem.SetProjection("EPSG:3857")
        clip = os.path.join(work, "mask.gpkg")
        gdal.VectorTranslate(clip, a.osm, spatFilter=list(b), format="GPKG")
        vds = ogr.Open(clip)
        for i in range(vds.GetLayerCount()):
            gdal.RasterizeLayer(mem, [1], vds.GetLayer(i), burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        vds = None
        mask = mem.GetRasterBand(1).ReadAsArray().astype(bool)
        mem = None
        os.remove(clip)

    out = os.path.join(work, f"block_{bx}_{by}.tif")
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out, px, px, 3, gdal.GDT_Byte,
                    ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"])
    ds.SetGeoTransform((b[0], res, 0, b[3], 0, -res))
    ds.SetProjection("EPSG:3857")
    strip = 2048
    stats = np.zeros(4)
    top = 0.0
    for r0 in range(0, px, strip):
        rows = min(strip, px - r0)
        sb = (b[0], b[3] - (r0 + rows) * res, b[2], b[3] - r0 * res)
        w = gdal.Warp("", src, format="MEM", dstSRS="EPSG:3857", outputBounds=sb, width=px, height=rows,
                      resampleAlg="bilinear", srcNodata=nodata, dstNodata=-9999, outputType=gdal.GDT_Float32)
        h = w.GetRasterBand(1).ReadAsArray()
        w = None
        valid = h > -9000
        if scale != 1.0:
            h = np.where(valid, h * scale, h)
        crown = valid & (h >= P["min_h"]) & (h < 125)
        if mask is not None:
            crown &= ~mask[r0:r0 + rows]
        b1 = np.where(crown, np.clip(np.rint(h * HS), 0, 254), 0).astype(np.uint8)
        b2 = np.where(crown, 255, 0).astype(np.uint8)
        ds.GetRasterBand(1).WriteArray(b1, 0, r0)
        ds.GetRasterBand(2).WriteArray(b2, 0, r0)
        ds.GetRasterBand(3).WriteArray(b1, 0, r0)
        stats += [valid.sum(), crown.sum(), np.where(crown, h, 0).sum(), (h[crown] >= 50).sum() if crown.any() else 0]
        top = max(top, float(h[crown].max())) if crown.any() else top
    src = None
    ds.FlushCache()
    print(f"    Raster {px} px: Daten {stats[0] / px / px * 100:.0f} %, Krone {stats[1] / max(stats[0], 1) * 100:.0f} %, "
          f"mittlere Kronenhöhe {stats[2] / max(stats[1], 1):.1f} m, >= 50 m: {int(stats[3])} px, "
          f"höchster Wert {top:.1f} m, {time.time() - t0:.0f} s", flush=True)
    if stats[0] == 0:
        ds = None
        os.remove(out)
        return 0
    levels = [2 ** i for i in range(1, a.maxzoom - z0 + 1)]
    ds.BuildOverviews("AVERAGE", levels)
    # Band 3: Übersichten mit dem Maximum statt dem Mittel überschreiben
    m = ds.GetRasterBand(3).ReadAsArray()
    for i in range(len(levels)):
        m = m.reshape(m.shape[0] // 2, 2, m.shape[1] // 2, 2).max(axis=(1, 3))
        ds.GetRasterBand(3).GetOverview(i).WriteArray(m)
    m256 = m.reshape(256, m.shape[0] // 256, 256, m.shape[1] // 256).max(axis=(1, 3))
    # Übersicht für die kleinen Zoomstufen: Blockkachel mit 256 px (= Auflösung von ZSPLIT-1)
    lr = os.path.join(a.lowres, f"{z0}_{bx}_{by}.tif")
    lds = gdal.GetDriverByName("GTiff").Create(lr, 256, 256, 3, gdal.GDT_Byte, ["COMPRESS=DEFLATE"])
    lds.SetGeoTransform((b[0], tile_size(z0) / 256, 0, b[3], 0, -tile_size(z0) / 256))
    lds.SetProjection("EPSG:3857")
    for k in (1, 2):
        lds.GetRasterBand(k).WriteArray(ds.GetRasterBand(k).ReadAsArray(0, 0, px, px, 256, 256,
                                                                        resample_alg=gdal.GRIORA_Average))
    lds.GetRasterBand(3).WriteArray(m256)
    lds = None
    ds = None

    jobs = []
    for z in range(z0, a.maxzoom + 1):
        k = 2 ** (z - z0)
        size = px // k
        for j in range(k):
            for i in range(k):
                jobs.append((out, z, bx * k + i, by * k + j, i * size, j * size, size, a.quality, a.palette))
    n = 0
    for z, x, y, data in pool.imap_unordered(_render_job, jobs, chunksize=8):
        if data:
            mb.add(z, x, y, data); n += 1
    mb.db.commit()
    os.remove(out)
    print(f"    {n}/{len(jobs)} Kacheln, {mb.bytes / 1e6:.0f} MB bisher, {time.time() - t0:.0f} s", flush=True)
    return n


def cmd_build(a):
    bl = blocks(a.bbox, a.zsplit)
    if a.partition == "contiguous":   # zusammenhängende Streifen: jeder Rechner braucht nur nahe Archivkacheln
        k0, k1 = len(bl) * a.shard // a.shards, len(bl) * (a.shard + 1) // a.shards
        mine = sorted(bl, key=lambda t: (t[0], t[1]))[k0:k1]
    else:
        mine = bl[a.shard::a.shards]
    print(f"Shard {a.shard}/{a.shards}: {len(mine)} von {len(bl)} Blockkacheln (z{a.zsplit})", flush=True)
    os.makedirs(a.lowres, exist_ok=True)
    cache = os.path.abspath(a.cache); work = os.path.abspath(a.work)
    os.makedirs(cache, exist_ok=True); os.makedirs(work, exist_ok=True)
    mb = MBTiles(a.out, metadata(a, a.zsplit, a.maxzoom))
    t0 = time.time()
    with Pool(os.cpu_count()) as pool:
        for i, (bx, by) in enumerate(mine):
            build_block(a, bx, by, cache, work, mb, pool)
            # Cache: nur Einheiten behalten, die die nächste Blockkachel braucht
            keep = set(lgl_units(tile_bounds(a.zsplit, *mine[i + 1]))) if i + 1 < len(mine) and a.source == "lgl" else set()
            for d in os.listdir(cache):
                try:
                    e, n = map(int, d.split("_"))
                except ValueError:
                    continue
                if (e, n) not in keep:
                    shutil.rmtree(os.path.join(cache, d), ignore_errors=True)
            print(f"  [{i + 1}/{len(mine)}] {(time.time() - t0) / 60:.1f} min", flush=True)
    mb.close()
    print(f"fertig: {mb.n} Kacheln, {mb.bytes / 1e6:.1f} MB", flush=True)


def cmd_overview(a):
    tifs = sorted(os.path.join(a.lowres, f) for f in os.listdir(a.lowres) if f.endswith(".tif"))
    print(f"{len(tifs)} Übersichten", flush=True)
    v12 = gdal.BuildVRT(os.path.abspath("lowres12.vrt"), tifs, bandList=[1, 2])
    v3 = gdal.BuildVRT(os.path.abspath("lowres3.vrt"), tifs, bandList=[3])
    zmax = a.zsplit - 1
    mb = MBTiles(a.out, metadata(a, a.minzoom, zmax))
    if True:
        for z in range(a.minzoom, zmax + 1):
            jobs = blocks(a.bbox, z)
            n = 0
            for x, y in jobs:
                tb = tile_bounds(z, x, y)
                # Ausserhalb der Übersichten bleibt 0 (= keine Krone); kein Nodata, damit 0 mitgemittelt wird
                w = gdal.Warp("", v12, format="MEM", outputBounds=tb, width=TILE, height=TILE, resampleAlg="average")
                b1 = w.GetRasterBand(1).ReadAsArray().astype(np.float32)
                b2 = w.GetRasterBand(2).ReadAsArray().astype(np.float32)
                w = gdal.Warp("", v3, format="MEM", outputBounds=tb, width=TILE, height=TILE, resampleAlg="max")
                b3 = w.GetRasterBand(1).ReadAsArray().astype(np.float32)
                rgba = colorize(b1, b2, b3)
                if rgba is not None:
                    mb.add(z, x, y, encode(rgba, a.quality)); n += 1
            print(f"  zoom {z}: {n}/{len(jobs)} Kacheln", flush=True)
    mb.close()
    print(f"fertig: {mb.n} Kacheln, {mb.bytes / 1e6:.1f} MB", flush=True)


def cmd_merge(a):
    """Fügt deduplizierte Teildateien zusammen; Metadaten aus der ersten, Zoombereich aus allen."""
    if os.path.exists(a.out):
        os.remove(a.out)
    db = sqlite3.connect(a.out)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);
    """)
    for i, p in enumerate(a.parts):
        db.execute("ATTACH DATABASE ? AS part", (p,))
        if i == 0:
            db.execute("INSERT INTO metadata SELECT * FROM part.metadata")
        db.execute("INSERT INTO map SELECT * FROM part.map")
        db.execute("INSERT OR IGNORE INTO images SELECT * FROM part.images")
        db.commit()
        db.execute("DETACH DATABASE part")
    db.executescript("""
        CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
            map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
    """)
    zmin, zmax = db.execute("SELECT min(zoom_level), max(zoom_level) FROM map").fetchone()
    db.execute("UPDATE metadata SET value=? WHERE name='minzoom'", (str(zmin),))
    db.execute("UPDATE metadata SET value=? WHERE name='maxzoom'", (str(zmax),))
    n_map, n_img = db.execute("SELECT count(*) FROM map").fetchone()[0], db.execute("SELECT count(*) FROM images").fetchone()[0]
    db.commit(); db.execute("VACUUM"); db.close()
    print(f"{a.out}: {n_map} Kacheln, {n_img} verschiedene, z{zmin}-{zmax}, {os.path.getsize(a.out) / 1e6:.1f} MB")


def cmd_pack(a):
    """Teildateien der Reihe nach zu möglichst wenigen Dateien unter --limit Bytes zusammenfassen."""
    import re
    parts = sorted(a.parts, key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", os.path.basename(p))])
    parts = [p for p in parts if "overview" in p] + [p for p in parts if "overview" not in p]
    groups, cur, size = [], [], 0
    for p in parts:
        sz = os.path.getsize(p)
        if cur and size + sz > a.limit:
            groups.append(cur); cur, size = [], 0
        cur.append(p); size += sz
    if cur:
        groups.append(cur)
    n = len(groups)
    for k, g in enumerate(groups, 1):
        out = f"{a.prefix}.mbtiles" if n == 1 else f"{a.prefix}-part{k}of{n}.mbtiles"
        a2 = argparse.Namespace(out=out, parts=g)
        cmd_merge(a2)


def cmd_legend(a):
    w, h = 520, 90
    top = P["legend_max"]
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    hs = np.linspace(0, top, w - 40)
    bar = np.zeros((26, w - 40, 4), np.uint8)
    for i, v in enumerate(hs):
        if v >= P["min_h"]:
            k = min(int(round(v * HS)), 255)
            bar[:, i, :3] = LUT[k]
            bar[:, i, 3] = int(LUTA[k] * 255)
    img.alpha_composite(Image.fromarray(bar, "RGBA"), (20, 20))
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    for v in P["ticks"]:
        x = 20 + int(v / top * (w - 41))
        d.line([(x, 46), (x, 52)], fill=(0, 0, 0))
        d.text((x - 6, 55), str(v), fill=(0, 0, 0), font=font)
    d.text((20, 72), f"Baumhöhe in m (unter {P['min_h']:g} m transparent)", fill=(0, 0, 0), font=font)
    img.save(a.out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--bbox", type=lambda s: [float(v) for v in s.split(",")], required=True)
        p.add_argument("--zsplit", type=int, default=11)
        p.add_argument("--minzoom", type=int, default=8)
        p.add_argument("--maxzoom", type=int, default=15)
        p.add_argument("--quality", type=int, default=80)
        p.add_argument("--title", default="Baumhöhe")
        p.add_argument("--out", required=True)
        p.add_argument("--lowres", required=True)
        p.add_argument("--palette", default="standard", choices=sorted(PALETTES))
        p.add_argument("--source", default="lgl")
        p.add_argument("--description", default="Baumhöhe (Kronenhöhe über Grund) aus dem nDOM1 des LGL BW; "
                                                 "Gebäude und Wasser nach OSM ausmaskiert")
        p.add_argument("--attribution", default="Baumhöhe: Datenquelle LGL, www.lgl-bw.de (nDOM1), dl-de/by-2-0, "
                                                 "Daten geändert; Maske © OpenStreetMap-Mitwirkende")

    p = sub.add_parser("blocks"); p.add_argument("--bbox", type=lambda s: [float(v) for v in s.split(",")], required=True)
    p.add_argument("--zsplit", type=int, default=11)
    p = sub.add_parser("build"); common(p)
    p.add_argument("--shard", type=int, default=0); p.add_argument("--shards", type=int, default=1)
    p.add_argument("--osm", default=None); p.add_argument("--cache", default="cache"); p.add_argument("--work", default="work")
    p.add_argument("--partition", default="roundrobin", choices=["roundrobin", "contiguous"])
    p = sub.add_parser("overview"); common(p)
    p = sub.add_parser("legend"); p.add_argument("out"); p.add_argument("--palette", default="standard")
    p = sub.add_parser("merge"); p.add_argument("out"); p.add_argument("parts", nargs="+")
    p = sub.add_parser("pack"); p.add_argument("prefix"); p.add_argument("parts", nargs="+")
    p.add_argument("--limit", type=int, default=1_900_000_000)
    a = ap.parse_args()
    if getattr(a, "palette", None):
        set_palette(a.palette)
    if a.cmd == "blocks":
        for x, y in blocks(a.bbox, a.zsplit):
            b = tile_bounds(a.zsplit, x, y)
            print(a.zsplit, x, y, len(lgl_units(b)))
    elif a.cmd == "build":
        cmd_build(a)
    elif a.cmd == "overview":
        cmd_overview(a)
    elif a.cmd == "legend":
        cmd_legend(a)
    elif a.cmd == "merge":
        cmd_merge(a)
    elif a.cmd == "pack":
        cmd_pack(a)


if __name__ == "__main__":
    main()
