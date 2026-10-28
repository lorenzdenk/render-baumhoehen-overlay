#!/usr/bin/env python3
"""Baumhöhen-Overlay aus dem nDOM1 des LGL Baden-Württemberg als MBTiles (512-px-WebP mit Transparenz).

Ablauf (skaliert bis ganz BW):
  Das Gebiet wird in Kacheln der Zoomstufe ZSPLIT (Standard 11, ca. 13 x 13 km) zerlegt. Jede dieser
  "Blockkacheln" wird für sich verarbeitet: nDOM-Kacheln laden, nach Web Mercator umrechnen, Gebäude und
  Wasser (OSM) ausmaskieren, Zoomstufen ZSPLIT..MAXZOOM rendern, Übersicht für die kleinen Zoomstufen
  speichern, aufräumen. Die Blockkacheln werden auf mehrere Rechner (Shards) verteilt.
  Danach rendert "overview" die Zoomstufen MINZOOM..ZSPLIT-1 aus den gespeicherten Übersichten.

Kodierung im Zwischenraster (2 Bänder, Byte):
  Band 1 = Baumhöhe * 4 (0,25-m-Schritte, 0 = keine Krone), Band 2 = 255 wo Krone (Höhe >= MIN_H), sonst 0.
  Beim Verkleinern werden beide Bänder gemittelt; mittlere Kronenhöhe = Band1 / Band2 * 255 / 4,
  Deckung = Band2 / 255 (wird als Transparenz verwendet).

Befehle:
  baumhoehe.py blocks  <W> <S> <E> <N> [--zsplit 11]               Blockkacheln des Gebiets auflisten
  baumhoehe.py build   --bbox W,S,E,N --shard K --shards N --out part.mbtiles --lowres DIR [--osm mask.gpkg]
  baumhoehe.py overview --lowres DIR --bbox ... --out overview.mbtiles
  baumhoehe.py legend  legend.png
"""
import argparse
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

# Farbskala Baumhöhe (m) -> RGB. Bewusst nicht grün, damit sie sich vom Wald der Grundkarte abhebt.
STOPS = [
    (2, (255, 255, 204)), (5, (199, 233, 180)), (10, (127, 205, 187)), (15, (65, 182, 196)),
    (20, (29, 145, 192)), (25, (34, 94, 168)), (30, (37, 52, 148)), (35, (8, 29, 88)), (45, (74, 20, 134)),
]
MIN_H = 2.0      # darunter: keine Krone (Gras, Felder, Rauschen)
ALPHA = 0.80     # Deckkraft bei voller Kronendeckung


def lut():
    """Farbtabelle für Band-1-Werte 0..255 (Höhe*4)."""
    hs = np.arange(256) / 4.0
    xs = [s[0] for s in STOPS]
    out = np.zeros((256, 3), np.uint8)
    for c in range(3):
        out[:, c] = np.interp(hs, xs, [s[1][c] for s in STOPS]).round().astype(np.uint8)
    return out


LUT = lut()


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


# ---------------------------------------------------------------- Rendern
def colorize(b1, b2):
    """b1, b2: gemittelte Bänder (float). Gibt RGBA uint8 zurück oder None, wenn leer."""
    cov = b2 / 255.0
    if not (cov > 0.004).any():
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        h4 = np.where(b2 > 0, b1 / np.maximum(b2, 1e-6) * 255.0, 0)
    idx = np.clip(np.rint(h4), 0, 255).astype(np.uint8)
    rgba = np.empty(b1.shape + (4,), np.uint8)
    rgba[..., :3] = LUT[idx]
    rgba[..., 3] = np.clip(np.rint(cov * ALPHA * 255), 0, 255).astype(np.uint8)
    return rgba


def encode(rgba, quality):
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=quality, method=4)
    return buf.getvalue()


_ds = None


def _render_job(args):
    """Liest ein Fenster aus dem 2-Band-Raster (mit Übersichten) und gibt (z, x, y, webp) zurück."""
    global _ds
    path, z, x, y, xoff, yoff, size, quality = args
    if _ds is None or _ds.GetDescription() != path:
        _ds = gdal.Open(path)
    b1 = _ds.GetRasterBand(1).ReadAsArray(xoff, yoff, size, size, TILE, TILE,
                                          resample_alg=gdal.GRIORA_Average).astype(np.float32)
    b2 = _ds.GetRasterBand(2).ReadAsArray(xoff, yoff, size, size, TILE, TILE,
                                          resample_alg=gdal.GRIORA_Average).astype(np.float32)
    rgba = colorize(b1, b2)
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
            "description": "Baumhöhe (Kronenhöhe über Grund) aus dem nDOM1 des LGL BW; Gebäude und Wasser nach OSM ausmaskiert",
            "attribution": "Baumhöhe: Datenquelle LGL, www.lgl-bw.de (nDOM1), dl-de/by-2-0, Daten geändert; Maske © OpenStreetMap-Mitwirkende",
            "minzoom": str(minzoom), "maxzoom": str(maxzoom), "bounds": f"{w},{s},{e},{n}",
            "center": f"{(w + e) / 2},{(s + n) / 2},{min(maxzoom, 13)}", "tile_size": str(TILE), "scale": "2"}


# ---------------------------------------------------------------- Blockkachel verarbeiten
def build_block(a, bx, by, cache, work, mb, pool):
    z0 = a.zsplit
    b = tile_bounds(z0, bx, by)
    px = TILE * 2 ** (a.maxzoom - z0)              # Pixel pro Seite im Zwischenraster
    res = tile_size(z0) / px
    t0 = time.time()

    units = lgl_units(b)
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(download_unit, [(u, cache) for u in units]))
    if any(f is None for _, f in got):
        raise RuntimeError("Download fehlgeschlagen")
    tifs = [t for _, f in got for t in f]
    print(f"  Block {z0}/{bx}/{by}: {len(units)} Einheiten, {len(tifs)} nDOM-Kacheln, "
          f"Download {time.time() - t0:.0f} s", flush=True)
    if not tifs:
        return 0

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
    ds = drv.Create(out, px, px, 2, gdal.GDT_Byte,
                    ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"])
    ds.SetGeoTransform((b[0], res, 0, b[3], 0, -res))
    ds.SetProjection("EPSG:3857")
    strip = 2048
    stats = np.zeros(4)
    for r0 in range(0, px, strip):
        rows = min(strip, px - r0)
        sb = (b[0], b[3] - (r0 + rows) * res, b[2], b[3] - r0 * res)
        w = gdal.Warp("", src, format="MEM", dstSRS="EPSG:3857", outputBounds=sb, width=px, height=rows,
                      resampleAlg="bilinear", srcNodata=nodata, dstNodata=-9999, outputType=gdal.GDT_Float32)
        h = w.GetRasterBand(1).ReadAsArray()
        w = None
        valid = h > -9000
        crown = valid & (h >= MIN_H)
        if mask is not None:
            crown &= ~mask[r0:r0 + rows]
        b1 = np.where(crown, np.clip(np.rint(h * 4), 0, 252), 0).astype(np.uint8)
        b2 = np.where(crown, 255, 0).astype(np.uint8)
        ds.GetRasterBand(1).WriteArray(b1, 0, r0)
        ds.GetRasterBand(2).WriteArray(b2, 0, r0)
        stats += [valid.sum(), crown.sum(), np.where(crown, h, 0).sum(), (h[crown] > 50).sum() if crown.any() else 0]
    src = None
    ds.FlushCache()
    ds.BuildOverviews("AVERAGE", [2 ** i for i in range(1, a.maxzoom - z0 + 1)])
    ds = None
    print(f"    Raster {px} px: Daten {stats[0] / px / px * 100:.0f} %, Krone {stats[1] / max(stats[0], 1) * 100:.0f} %, "
          f"mittlere Kronenhöhe {stats[2] / max(stats[1], 1):.1f} m, > 50 m: {int(stats[3])} px, {time.time() - t0:.0f} s",
          flush=True)

    # Übersicht für die kleinen Zoomstufen: Blockkachel mit 256 px (= Auflösung von ZSPLIT-1)
    lr = os.path.join(a.lowres, f"{z0}_{bx}_{by}.tif")
    gdal.Translate(lr, out, width=256, height=256, resampleAlg="average", creationOptions=["COMPRESS=DEFLATE"])

    jobs = []
    for z in range(z0, a.maxzoom + 1):
        k = 2 ** (z - z0)
        size = px // k
        for j in range(k):
            for i in range(k):
                jobs.append((out, z, bx * k + i, by * k + j, i * size, j * size, size, a.quality))
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
            keep = set(lgl_units(tile_bounds(a.zsplit, *mine[i + 1]))) if i + 1 < len(mine) else set()
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
    vrt = os.path.abspath("lowres.vrt")
    gdal.BuildVRT(vrt, tifs)
    zmax = a.zsplit - 1
    ds = gdal.Open(vrt)
    mb = MBTiles(a.out, metadata(a, a.minzoom, zmax))
    if True:
        for z in range(a.minzoom, zmax + 1):
            jobs = blocks(a.bbox, z)
            n = 0
            for x, y in jobs:
                tb = tile_bounds(z, x, y)
                # Ausserhalb der Übersichten wird mit 0 (= keine Krone) aufgefüllt; kein Nodata, damit 0 mitgemittelt wird
                w = gdal.Translate("", ds, format="MEM", projWin=[tb[0], tb[3], tb[2], tb[1]], width=TILE, height=TILE,
                                   resampleAlg="average", noData="none")
                b1 = w.GetRasterBand(1).ReadAsArray().astype(np.float32)
                b2 = w.GetRasterBand(2).ReadAsArray().astype(np.float32)
                rgba = colorize(b1, b2)
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


def cmd_legend(a):
    w, h = 520, 90
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    hs = np.linspace(0, 45, w - 40)
    bar = np.zeros((26, w - 40, 4), np.uint8)
    for i, v in enumerate(hs):
        if v >= MIN_H:
            bar[:, i, :3] = LUT[min(int(round(v * 4)), 255)]
            bar[:, i, 3] = int(ALPHA * 255)
    img.alpha_composite(Image.fromarray(bar, "RGBA"), (20, 20))
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    for v in (2, 5, 10, 15, 20, 25, 30, 35, 40, 45):
        x = 20 + int(v / 45 * (w - 41))
        d.line([(x, 46), (x, 52)], fill=(0, 0, 0))
        d.text((x - 6, 55), str(v), fill=(0, 0, 0), font=font)
    d.text((20, 72), "Baumhöhe in m (unter 2 m transparent)", fill=(0, 0, 0), font=font)
    img.save(a.out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--bbox", type=lambda s: [float(v) for v in s.split(",")], required=True)
        p.add_argument("--zsplit", type=int, default=11)
        p.add_argument("--minzoom", type=int, default=8)
        p.add_argument("--maxzoom", type=int, default=16)
        p.add_argument("--quality", type=int, default=80)
        p.add_argument("--title", default="Baumhöhe")
        p.add_argument("--out", required=True)
        p.add_argument("--lowres", required=True)

    p = sub.add_parser("blocks"); p.add_argument("--bbox", type=lambda s: [float(v) for v in s.split(",")], required=True)
    p.add_argument("--zsplit", type=int, default=11)
    p = sub.add_parser("build"); common(p)
    p.add_argument("--shard", type=int, default=0); p.add_argument("--shards", type=int, default=1)
    p.add_argument("--osm", default=None); p.add_argument("--cache", default="cache"); p.add_argument("--work", default="work")
    p = sub.add_parser("overview"); common(p)
    p = sub.add_parser("legend"); p.add_argument("out")
    p = sub.add_parser("merge"); p.add_argument("out"); p.add_argument("parts", nargs="+")
    a = ap.parse_args()
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


if __name__ == "__main__":
    main()
