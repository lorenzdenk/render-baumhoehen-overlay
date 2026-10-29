#!/usr/bin/env python3
"""Baumliste für das BW-Zielscheiben-Overlay aus den Kandidaten von bw_scan.py.
Höhe = Laser (DOM1 - DGM1), sonst nDOM. Bauwerke wie in bw_top.py: Gruppen (60 m Kette) mit einem Wert über
--max m oder Spitzen auf einer Geraden werden verworfen, ebenso flache Spitzen (Laserkrone 29/29 px, Dach) und
Spitzen ohne Krone im nDOM (< 8 px).
usage: bw_trees.py <out.csv> cand_*.csv [--min 50] [--max 72]"""
import argparse
import csv

import numpy as np
from scipy.spatial import cKDTree

ap = argparse.ArgumentParser()
ap.add_argument("out"); ap.add_argument("files", nargs="+")
ap.add_argument("--min", type=float, default=50); ap.add_argument("--max", type=float, default=72)
a = ap.parse_args()
rows = [r for f in a.files for r in csv.DictReader(open(f)) if r["verworfen"] == ""]
xy = np.array([[float(r["x"]), float(r["y"])] for r in rows])
nd = np.array([float(r["ndom_m"]) for r in rows])
li = np.array([float(r["lidar_m"]) if r["lidar_m"] else np.nan for r in rows])
h = np.where(np.isnan(li), nd, li)
hmax = np.fmax(nd, np.nan_to_num(li, nan=0))
sel = np.nonzero(hmax >= a.min - 5)[0]
par = np.arange(len(sel))


def find(i):
    while par[i] != i:
        par[i] = par[par[i]]; i = par[i]
    return i


for p, q in cKDTree(xy[sel]).query_pairs(60):
    rp, rq = find(p), find(q)
    if rp != rq:
        par[max(rp, rq)] = min(rp, rq)
lab = np.array([find(i) for i in range(len(sel))])
bad = set()
for g in np.unique(lab):
    m = sel[lab == g]
    if hmax[m].max() > a.max:
        bad.add(g); continue
    if len(m) >= 5:
        d = xy[m] - xy[m].mean(0)
        ev = np.sort(np.linalg.eigvalsh(np.cov(d.T)))
        if np.sqrt(ev[1] / max(ev[0], 0.25)) >= 6 and 2 * np.sqrt(ev[1]) >= 30:
            bad.add(g)
out, seen = [], cKDTree(np.zeros((0, 2))) if False else None
keep = []
for k, i in enumerate(sel):
    r = rows[i]
    if lab[k] in bad or h[i] < a.min:
        continue
    if r["lidar_crown_px"] and int(r["lidar_crown_px"]) >= 29:
        continue
    if int(r["crown_px"]) < 8:
        continue
    keep.append(i)
# doppelte Spitzen (Einheitenränder) entfernen: höchste behalten
keep.sort(key=lambda i: -h[i])
kd = cKDTree(xy[keep]) if keep else None
drop = set()
for n_, i in enumerate(keep):
    if n_ in drop:
        continue
    for j in kd.query_ball_point(xy[i], 3):
        if j != n_:
            drop.add(j)
with open(a.out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["lon", "lat", "height_m", "ndom_m", "lidar_m", "x", "y"])
    for n_, i in enumerate(keep):
        if n_ not in drop:
            r = rows[i]
            w.writerow([r["lon"], r["lat"], round(float(h[i]), 1), r["ndom_m"], r["lidar_m"], r["x"], r["y"]])
hs = np.array([h[i] for n_, i in enumerate(keep) if n_ not in drop])
print(f"{len(hs)} Bäume >= {a.min:g} m (>= 55: {(hs >= 55).sum()}, >= 60: {(hs >= 60).sum()}, >= 65: {(hs >= 65).sum()}); "
      f"{len(bad)} Gruppen als Bauwerk/Leitung verworfen")
