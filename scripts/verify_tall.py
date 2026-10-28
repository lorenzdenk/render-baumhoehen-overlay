#!/usr/bin/env python3
"""Formprüfung hoher Baumspitzen auf den Archivkacheln (chm_archive.py):
Um jede Spitze ab --min m wird der zusammenhängende Bereich "höchstens 5 m unter der Spitze" (8er-Nachbarschaft,
Fenster 50 x 50 m) betrachtet. Ein Leitungsseil über einem Tal ergibt eine lange, schmale Linie, eine Krone einen
rundlichen Fleck. Verworfen werden Spitzen mit Streckung >= --elong (Wurzel des Eigenwertverhältnisses) und
Länge >= 12 m, sowie Spitzen, die mehr als --jump m über dem 90-%-Wert ihrer Umgebung (15-30 m) stehen.

usage: verify_tall.py <trees.csv> <index.csv-URL oder Ordner> <out ok.csv> <out wires.csv> [--min 70]
"""
import argparse
import csv
import os
import sys
import urllib.request
from collections import defaultdict

import numpy as np
from osgeo import gdal
from scipy import ndimage

gdal.UseExceptions()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trees"); ap.add_argument("index"); ap.add_argument("ok"); ap.add_argument("bad")
    ap.add_argument("--min", type=float, default=70)
    ap.add_argument("--elong", type=float, default=3.5)
    ap.add_argument("--jump", type=float, default=45)
    ap.add_argument("--cache", default="vcache")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    if a.index.startswith("http"):
        with urllib.request.urlopen(a.index) as r:
            idx = list(csv.DictReader(r.read().decode().splitlines()))
        base = a.index.rsplit("/", 1)[0]
    else:
        idx = list(csv.DictReader(open(os.path.join(a.index, "index.csv"))))
        base = a.index
    rows = list(csv.DictReader(open(a.trees)))
    tall = [i for i, r in enumerate(rows) if float(r["height_m"]) >= a.min]
    by_tile = defaultdict(list)
    for i in tall:
        x, y = float(rows[i]["x"]), float(rows[i]["y"])
        for t in idx:
            if float(t["minx"]) <= x < float(t["maxx"]) and float(t["miny"]) <= y < float(t["maxy"]):
                by_tile[t["file"]].append(i); break
    print(f"{len(rows)} Spitzen, {len(tall)} ab {a.min:g} m in {len(by_tile)} Kacheln", flush=True)
    bad = {}
    yy, xx = np.mgrid[-15:16, -15:16]
    ring = (xx * xx + yy * yy >= 7.5 ** 2) & (xx * xx + yy * yy <= 15 ** 2)   # 15-30 m bei 2 m Pixeln
    for k, (f, ids) in enumerate(sorted(by_tile.items()), 1):
        p = os.path.join(a.cache, f)
        if not os.path.exists(p):
            if base.startswith("http"):
                urllib.request.urlretrieve(f"{base}/{f}", p)
            else:
                p = os.path.join(base, f)
        ds = gdal.Open(p)
        gt = ds.GetGeoTransform()
        arr = ds.GetRasterBand(1).ReadAsArray()
        h = np.where(arr == 255, np.nan, arr / 2.0).astype(np.float32)
        h = np.pad(h, 16, constant_values=np.nan)
        for i in ids:
            r = rows[i]
            c = int((float(r["x"]) - gt[0]) / gt[1]) + 16
            l = int((gt[3] - float(r["y"])) / -gt[5]) + 16
            w = h[l - 15:l + 16, c - 15:c + 16]
            if w.shape != (31, 31):
                continue
            top = float(np.nanmax(w[13:18, 13:18])) if np.isfinite(w[13:18, 13:18]).any() else float(r["height_m"])
            blob = np.nan_to_num(w, nan=0) >= top - 5
            lab, _ = ndimage.label(blob, structure=np.ones((3, 3)))
            k0 = lab[15, 15] or (lab[13:18, 13:18].max())
            ys, xs = np.nonzero(lab == k0) if k0 else (np.array([15]), np.array([15]))
            if len(xs) >= 3:
                cov = np.cov(np.c_[xs, ys].T)
                ev = np.sort(np.linalg.eigvalsh(cov))
                elong = float(np.sqrt(ev[1] / max(ev[0], 0.05)))
                length = float(2 * np.sqrt(ev[1]) * 2 * 2)   # ~4 Standardabweichungen in m
            else:
                elong, length = 1.0, 0.0
            sur = w[ring]
            sur = sur[np.isfinite(sur)]
            p90 = float(np.percentile(sur, 90)) if len(sur) else 0.0
            reason = ""
            if elong >= a.elong and length >= 12:
                reason = "linie"
            elif top - p90 > a.jump and len(xs) < 8:
                reason = "einzelspitze"
            r["elong"], r["umgebung_p90"], r["krone_px"] = round(elong, 2), round(p90, 1), int(len(xs))
            if reason:
                bad[i] = reason
        if k % 10 == 0:
            print(f"  {k}/{len(by_tile)} Kacheln, {len(bad)} verworfen", flush=True)
    # Ketten: viele hohe Spitzen auf einer Geraden (Leitungsseil) -> alle verwerfen
    from scipy.spatial import cKDTree
    ti = np.array(tall)
    xy = np.array([[float(rows[i]["x"]), float(rows[i]["y"])] for i in tall])
    kd = cKDTree(xy)
    for n_, nb in enumerate(kd.query_ball_point(xy, 60)):
        if len(nb) >= 5:
            q = xy[nb] - xy[nb].mean(axis=0)
            ev = np.sort(np.linalg.eigvalsh(np.cov(q.T)))
            if ev[1] > 0 and np.sqrt(ev[1] / max(ev[0], 0.25)) >= 6 and 2 * np.sqrt(ev[1]) >= 30:
                for j in nb:
                    bad.setdefault(int(ti[j]), "kette")
    with open(a.ok, "w", newline="") as fo, open(a.bad, "w", newline="") as fb:
        keys = list(rows[0].keys()) + (["elong", "umgebung_p90", "krone_px"] if "elong" not in rows[0] else [])
        wo = csv.DictWriter(fo, fieldnames=keys, extrasaction="ignore"); wo.writeheader()
        wb = csv.DictWriter(fb, fieldnames=keys + ["grund"], extrasaction="ignore"); wb.writeheader()
        for i, r in enumerate(rows):
            if i in bad:
                wb.writerow(dict(r, grund=bad[i]))
            else:
                wo.writerow(r)
    hs = np.array([float(rows[i]["height_m"]) for i in bad]) if bad else np.zeros(0)
    print(f"verworfen: {len(bad)} (davon >= 90 m: {(hs >= 90).sum()}, >= 100 m: {(hs >= 100).sum()})")


if __name__ == "__main__":
    main()
