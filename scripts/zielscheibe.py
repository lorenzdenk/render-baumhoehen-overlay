#!/usr/bin/env python3
"""Zielscheiben-Overlay für hohe Bäume als MBTiles (512-px-WebP mit Transparenz).

Eingabe: trees.csv (lon, lat, height_m, ...) aus chm_archive.py, nur Bäume >= --min (Standard 50 m).
Darstellung:
  - jeder Baum als Punkt, Farbe = Höhe (50 m gelb -> 100 m dunkelviolett), dunkler Rand
  - Ringe in 100 / 250 / 500 m Abstand zum nächsten hohen Baum (folgen der Form der Gruppe);
    weit herausgezoomt mindestens 7 / 13 / 19 px, damit sie sichtbar bleiben; Kernzone leicht getönt
  - Beschriftung je Gruppe: Höhe des höchsten Baums; ab z14 zusätzlich einzelne Bäume ab 60 m
  - Beschriftungen werden pro Zoomstufe global (für das ganze Gebiet) platziert, ohne Überlappung und
    über Kachelgrenzen hinweg (jede Kachel wird mit Rand gezeichnet und dann zugeschnitten)

usage: zielscheibe.py <trees.csv> <out.mbtiles> [--minzoom 8] [--maxzoom 14] [--title ...] [--attribution ...]
"""
import argparse
import csv
import hashlib
import io
import math
import os
import sqlite3
import time
from multiprocessing import Pool

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage
from scipy.spatial import cKDTree

R = 20037508.342789244
TILE = 512
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
STOPS = [(50, (255, 225, 0)), (60, (255, 140, 0)), (70, (225, 25, 25)), (80, (200, 0, 150)), (90, (110, 0, 170)),
         (100, (40, 0, 90))]
RING = (230, 60, 20)
RINGS_M = (100, 250, 500)
RINGS_MINPX = (7, 13, 19)
RING_W = (2.6, 2.0, 1.6)
RING_A = (230, 160, 100)
CORE_A = 40
CROWN_M = 7.0          # angenommener Kronenradius für die Darstellung
TREE_LABEL_MIN = 60    # ab z14 einzelne Bäume beschriften


def hcol(h):
    xs = [s[0] for s in STOPS]
    return tuple(int(np.interp(h, xs, [s[1][c] for s in STOPS])) for c in range(3))


def merc(lon, lat):
    x = np.radians(lon) * 6378137.0
    y = np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * 6378137.0
    return x, y


def res(z):
    return 2 * R / 2 ** z / TILE  # Mercator-Meter pro Pixel


def radii_px(z, lat):
    g = res(z) * math.cos(math.radians(lat))  # Bodenmeter pro Pixel
    return [max(rm / g, mp) for rm, mp in zip(RINGS_M, RINGS_MINPX)], g


def clusters(xy, dist):
    """Gruppen über ein Raster (schnell auch für Millionen Punkte): Punkte, deren Abstand höchstens etwa
    dist beträgt, landen in derselben Gruppe (Zellen dist/3, um eine Zelle erweitert, 8er-Nachbarschaft)."""
    from scipy import ndimage as nd
    cell = dist / 3.0
    ij = np.floor((xy - xy.min(axis=0)) / cell).astype(np.int64) + 1
    shape = ij.max(axis=0) + 2
    occ = np.zeros((shape[1], shape[0]), bool)
    occ[ij[:, 1], ij[:, 0]] = True
    occ = nd.binary_dilation(occ, structure=np.ones((3, 3), bool))
    lab, _ = nd.label(occ, structure=np.ones((3, 3), bool))
    return lab[ij[:, 1], ij[:, 0]]


class Boxes:
    """Belegte Beschriftungsflächen mit Rasterindex (schnelle Kollisionsprüfung)."""

    def __init__(self, cell=64):
        self.cell, self.g = cell, {}

    def _cells(self, b):
        c = self.cell
        for i in range(int(b[0] // c), int(b[2] // c) + 1):
            for j in range(int(b[1] // c), int(b[3] // c) + 1):
                yield i, j

    def free(self, b):
        for k in self._cells(b):
            for c in self.g.get(k, ()):
                if b[0] < c[2] and b[2] > c[0] and b[1] < c[3] and b[3] > c[1]:
                    return False
        return True

    def add(self, b):
        for k in self._cells(b):
            self.g.setdefault(k, []).append(b)


# ---------------------------------------------------------------- Beschriftungen (global je Zoomstufe)
def place_labels(X, Y, H, lat0, z, font, fonts):
    """Gibt Liste (x_px, y_px, text, kind) in globalen Pixelkoordinaten der Zoomstufe zurück."""
    r = res(z)
    gx, gy = (X + R) / r, (R - Y) / r
    rad, g = radii_px(z, lat0)
    lab = clusters(np.c_[gx, gy], 2 * rad[1])   # Gruppen = zusammenhängende 250-m-Zonen
    out, boxes = [], Boxes()
    srt = np.argsort(lab, kind="stable")
    cuts = np.nonzero(np.diff(lab[srt]))[0] + 1
    groups = np.split(srt, cuts)
    order = sorted(groups, key=lambda m: -H[m].max())
    dummy = ImageDraw.Draw(Image.new("L", (1, 1)))
    free = boxes.free

    for m in order:
        top = m[np.argmax(H[m])]
        txt = f"{H[top]:.0f} m"
        bb = dummy.textbbox((0, 0), txt, font=font, stroke_width=3)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        # Kandidaten: oben rechts, unten rechts, oben links, unten links ausserhalb des 250-m-Rings
        x1, y0 = gx[m].max() + rad[1] * 0.75, gy[m].min() - rad[1] * 0.75
        x0, y1 = gx[m].min() - rad[1] * 0.75, gy[m].max() + rad[1] * 0.75
        for (px, py) in ((x1, y0 - h), (x1, y1), (x0 - w, y0 - h), (x0 - w, y1)):
            b = (px - 2, py - 2, px + w + 2, py + h + 2)
            if free(b):
                boxes.add(b); out.append((px, py, txt, "group"))
                break
    if z >= 14:
        idx = np.argsort(-H)
        for i in idx:
            if H[i] < TREE_LABEL_MIN:
                break
            txt = f"{H[i]:.0f}"
            bb = dummy.textbbox((0, 0), txt, font=fonts, stroke_width=2)
            px, py = gx[i] + 6, gy[i] - 6
            b = (px - 1, py - 1, px + bb[2] + 1, py + bb[3] + 1)
            if free(b):
                boxes.add(b); out.append((px, py, txt, "tree"))
    return out


# ---------------------------------------------------------------- Kachel zeichnen
G = {}


def init(X, Y, H, labels_by_zoom):
    G["X"], G["Y"], G["H"] = X, Y, H
    G["tree"] = cKDTree(np.c_[X, Y])
    G["labels"] = {z: (np.array([[l[0], l[1]] for l in L]) if L else np.zeros((0, 2)), L) for z, L in labels_by_zoom.items()}
    G["font"] = {z: ImageFont.truetype(FONTB, 13 if z < 14 else 14) for z in labels_by_zoom}
    G["fonts"] = ImageFont.truetype(FONT, 11)


def render(args):
    z, x, y, quality = args
    r = res(z)
    cx, cy = -R + (x + 0.5) * TILE * r, R - (y + 0.5) * TILE * r
    lat = math.degrees(2 * math.atan(math.exp(cy / 6378137.0)) - math.pi / 2)
    rad, g = radii_px(z, lat)
    B = int(math.ceil(rad[2] + 12))
    S = TILE + 2 * B
    ox, oy = x * TILE - B, y * TILE - B          # globale Pixel der linken oberen Ecke
    near = G["tree"].query_ball_point([cx, cy], (S / 2 * 1.5) * r)
    X, Y, H = G["X"][near], G["Y"][near], G["H"][near]
    px, py = (X + R) / r - ox, (R - Y) / r - oy
    cr = max(CROWN_M / g, 2.5 if z >= 12 else 2.0)
    mask = np.zeros((S, S), bool)
    rr = int(math.ceil(cr))
    yy, xx = np.mgrid[-rr:rr + 1, -rr:rr + 1]
    disc = xx * xx + yy * yy <= cr * cr
    for a, b in zip(px, py):
        ia, ib = int(round(a)), int(round(b))
        if -rr <= ia < S + rr and -rr <= ib < S + rr:
            x0, y0 = max(ia - rr, 0), max(ib - rr, 0)
            x1, y1 = min(ia + rr + 1, S), min(ib + rr + 1, S)
            mask[y0:y1, x0:x1] |= disc[y0 - (ib - rr):y1 - (ib - rr), x0 - (ia - rr):x1 - (ia - rr)]
    rgba = np.zeros((S, S, 4), np.uint8)
    if mask.any():
        dist = ndimage.distance_transform_edt(~mask)
        rgba[dist <= rad[0]] = (*RING, CORE_A)
        for rr_, w, a in zip(rad, RING_W, RING_A):
            rgba[np.abs(dist - rr_) <= w / 2] = (*RING, a)
    img = Image.fromarray(rgba, "RGBA")
    d = ImageDraw.Draw(img)
    # Punkte ausdünnen: je Rasterzelle nur der höchste Baum (ab z14 alle)
    cell = {8: 10, 9: 10, 10: 10, 11: 10, 12: 7, 13: 5}.get(z, 0)
    order = np.argsort(-H)
    if cell and len(order):
        key = (np.floor(px[order] / cell).astype(np.int64) * 100003 + np.floor(py[order] / cell).astype(np.int64))
        _, first = np.unique(key, return_index=True)
        order = order[first]
    for i in order[np.argsort(H[order])]:
        a, b = px[i], py[i]
        if -10 <= a < S + 10 and -10 <= b < S + 10:
            d.ellipse([a - cr, b - cr, a + cr, b + cr], fill=hcol(H[i]) + (255,), outline=(40, 20, 20, 255), width=1)
    pts, L = G["labels"][z]
    if len(pts):
        sel = np.nonzero((pts[:, 0] > ox - 200) & (pts[:, 0] < ox + S) & (pts[:, 1] > oy - 40) & (pts[:, 1] < oy + S))[0]
        for k in sel:
            lx, ly, txt, kind = L[k]
            if kind == "group":
                d.text((lx - ox, ly - oy), txt, font=G["font"][z], fill=(150, 20, 0, 255), stroke_width=3,
                       stroke_fill=(255, 255, 255, 255))
            else:
                d.text((lx - ox, ly - oy), txt, font=G["fonts"], fill=(60, 0, 0, 255), stroke_width=2,
                       stroke_fill=(255, 255, 255, 255))
    tile = img.crop((B, B, B + TILE, B + TILE))
    if tile.getextrema()[3][1] == 0:
        return z, x, y, None
    buf = io.BytesIO()
    tile.save(buf, "WEBP", quality=quality, method=4)
    return z, x, y, buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trees"); ap.add_argument("out")
    ap.add_argument("--min", type=float, default=50)
    ap.add_argument("--max", type=float, default=102, help="höher = Messfehler (höchster bekannter Baum Tasmaniens: 100,5 m)")
    ap.add_argument("--minzoom", type=int, default=8); ap.add_argument("--maxzoom", type=int, default=14)
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--title", default="Riesenbäume")
    ap.add_argument("--attribution", default="")
    ap.add_argument("--description", default="Bäume ab 50 m (Punkt, Farbe = Höhe) mit Ringen in 100/250/500 m Abstand")
    a = ap.parse_args()
    lon, lat, h = [], [], []
    with open(a.trees) as f:
        for row in csv.DictReader(f):
            if a.min <= float(row["height_m"]) <= a.max:
                lon.append(float(row["lon"])); lat.append(float(row["lat"])); h.append(float(row["height_m"]))
    lon, lat, H = np.array(lon), np.array(lat), np.array(h)
    X, Y = merc(lon, lat)
    print(f"{len(H)} Bäume >= {a.min:g} m, höchster {H.max():.1f} m", flush=True)
    lat0 = float(np.median(lat))
    t0 = time.time()
    labels = {}
    for z in range(a.minzoom, a.maxzoom + 1):
        labels[z] = place_labels(X, Y, H, lat0, z, ImageFont.truetype(FONTB, 13 if z < 14 else 14),
                                 ImageFont.truetype(FONT, 11))
        print(f"  z{z}: {len(labels[z])} Beschriftungen", flush=True)
    if os.path.exists(a.out):
        os.remove(a.out)
    db = sqlite3.connect(a.out)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);
        CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
            map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
    """)
    w, s, e, n = lon.min(), lat.min(), lon.max(), lat.max()
    meta = {"name": a.title, "format": "webp", "type": "overlay", "version": "1", "description": a.description,
            "attribution": a.attribution, "minzoom": str(a.minzoom), "maxzoom": str(a.maxzoom),
            "bounds": f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}", "center": f"{(w + e) / 2:.4f},{(s + n) / 2:.4f},10",
            "tile_size": str(TILE), "scale": "2"}
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    total = size = 0
    with Pool(os.cpu_count(), initializer=init, initargs=(X, Y, H, labels)) as pool:
        for z in range(a.minzoom, a.maxzoom + 1):
            r = res(z)
            rad, _ = radii_px(z, lat0)
            reach = (rad[2] + 40) * r
            step = TILE * r
            tiles = set()
            for dx in (-reach, 0, reach):
                for dy in (-reach, 0, reach):
                    tx = ((X + dx + R) // step).astype(np.int64)
                    ty = ((R - Y - dy) // step).astype(np.int64)
                    tiles.update(zip(*np.unique(np.c_[tx, ty], axis=0).T.tolist()))
            # Kacheln, in die eine Beschriftung hineinragt
            for lx, ly, txt, kind in labels[z]:
                for gx in (lx - 4, lx + 12 * len(txt)):
                    for gy in (ly - 4, ly + 22):
                        tiles.add((int(gx // TILE), int(gy // TILE)))
            jobs = [(z, tx, ty, a.quality) for tx, ty in sorted(tiles)]
            got = 0
            for zz, tx, ty, data in pool.imap_unordered(render, jobs, chunksize=16):
                if data:
                    hh = hashlib.md5(data).hexdigest()
                    if db.execute("INSERT OR IGNORE INTO images VALUES (?, ?)", (hh, data)).rowcount:
                        size += len(data)
                    db.execute("INSERT OR REPLACE INTO map VALUES (?, ?, ?, ?)", (zz, tx, (1 << zz) - 1 - ty, hh))
                    got += 1
            db.commit()
            total += got
            print(f"  z{z}: {got}/{len(jobs)} Kacheln, {size / 1e6:.1f} MB, {(time.time() - t0) / 60:.1f} min", flush=True)
    db.commit(); db.close()
    print(f"fertig: {total} Kacheln, {os.path.getsize(a.out) / 1e6:.1f} MB", flush=True)


def legend(path):
    from PIL import Image as I
    W = 960
    im = I.new("RGB", (W, 120), (255, 255, 255))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 13); fb = ImageFont.truetype(FONTB, 13)
    d.text((10, 6), "Baum ab 50 m (Farbe = Höhe):", font=fb, fill=(0, 0, 0))
    for i in range(300):
        d.line([(10 + i, 28), (10 + i, 46)], fill=hcol(50 + i / 300 * 50))
    for v in (50, 60, 70, 80, 90, 100):
        x = 10 + (v - 50) / 50 * 299
        d.line([(x, 46), (x, 51)], fill=(0, 0, 0)); d.text((x - 9, 53), f"{v}", font=f, fill=(0, 0, 0))
    x = 360
    d.text((x, 6), "Zielscheibe um jede Gruppe:", font=fb, fill=(0, 0, 0))
    cx, cy = x + 40, 70
    for r, w in ((38, 2), (26, 2), (14, 3)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=RING, width=w)
    d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=hcol(80), outline=(40, 20, 20))
    d.text((x + 90, 30), "Ringe in 100 m, 250 m und 500 m Abstand zum nächsten Baum ab 50 m;", font=f, fill=(0, 0, 0))
    d.text((x + 90, 48), "weit herausgezoomt mindestens 7 / 13 / 19 Pixel.", font=f, fill=(0, 0, 0))
    d.text((x + 90, 66), "Beschriftung: Höhe des höchsten Baums der Gruppe,", font=f, fill=(0, 0, 0))
    d.text((x + 90, 84), "ab Zoom 14 zusätzlich einzelne Bäume ab 60 m.", font=f, fill=(0, 0, 0))
    im.save(path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "legend":
        legend(sys.argv[2])
    else:
        main()
