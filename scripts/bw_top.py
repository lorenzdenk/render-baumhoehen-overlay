#!/usr/bin/env python3
"""Rangliste der höchsten Bäume aus den Kandidaten von bw_scan.py.

Gewertet werden nur per Laser nachgemessene Spitzen (DOM1 - DGM1). Bauwerke werden über die ganze Gruppe erkannt
(Spitzen mit höchstens 60 m Abstand, Kette):
  - irgendein Wert der Gruppe (Laser oder nDOM) über 72 m -> kein Baum (der höchste bekannte Baum Deutschlands
    misst rund 67 m); typisch für Masten, Türme, Brücken, Seile über Tälern
  - Spitzen auf einer Geraden (Leitung)
Je Spitze zusätzlich: Krone im nDOM >= 15 px und im Laser-DOM 10..27 von 29 px (29 = flach wie ein Dach).

usage: bw_top.py <out dir> cand_*.csv
"""
import csv
import math
import sys

import numpy as np
from scipy.spatial import cKDTree

out, files = sys.argv[1], sys.argv[2:]
rows = [r for f in files for r in csv.DictReader(open(f)) if r["verworfen"] == ""]
xy = np.array([[float(r["x"]), float(r["y"])] for r in rows])
nd = np.array([float(r["ndom_m"]) for r in rows])
li = np.array([float(r["lidar_m"]) if r["lidar_m"] else np.nan for r in rows])
hmax = np.fmax(nd, np.nan_to_num(li, nan=0))
sel = np.nonzero(hmax >= 50)[0]
par = np.arange(len(sel))


def find(i):
    while par[i] != i:
        par[i] = par[par[i]]
        i = par[i]
    return i


for a, b in cKDTree(xy[sel]).query_pairs(60):
    ra, rb = find(a), find(b)
    if ra != rb:
        par[max(ra, rb)] = min(ra, rb)
lab = np.array([find(i) for i in range(len(sel))])
bad = set()
for g in np.unique(lab):
    m = sel[lab == g]
    if hmax[m].max() > 72:
        bad.add(g); continue
    if len(m) >= 5:
        q = xy[m] - xy[m].mean(0)
        ev = np.sort(np.linalg.eigvalsh(np.cov(q.T)))
        if np.sqrt(ev[1] / max(ev[0], 0.25)) >= 6 and 2 * np.sqrt(ev[1]) >= 30:
            bad.add(g)
cand = []
for k, i in enumerate(sel):
    r = rows[i]
    if lab[k] in bad or np.isnan(li[i]) or not r["lidar_crown_px"]:
        continue
    if int(r["crown_px"]) >= 15 and 10 <= int(r["lidar_crown_px"]) <= 27:
        cand.append((i, int(lab[k])))
cand.sort(key=lambda t: -li[t[0]])
top = []
for i, g in cand:
    if all(math.hypot(*(xy[i] - xy[j])) > 25 for j, _ in top):
        top.append((i, g))
    if len(top) >= 50:
        break
keys = ["rang", "lat", "lon", "hoehe_laser_m", "hoehe_ndom_m", "befliegung", "bestand", "krone_ndom_px", "krone_laser_px",
        "wald_anteil", "utm32_x", "utm32_y"]
with open(f"{out}/bw_top50.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(keys)
    for k, (i, g) in enumerate(top, 1):
        r = rows[i]
        w.writerow([k, r["lat"], r["lon"], r["lidar_m"], r["ndom_m"], r["jahre"], g, r["crown_px"], r["lidar_crown_px"],
                    r["forest_frac"], r["x"], r["y"]])
with open(f"{out}/bw_top10.md", "w") as f:
    f.write("| Rang | Höhe Laser (DOM1−DGM1) | Höhe nDOM | Koordinaten | Befliegung |\n|---|---|---|---|---|\n")
    for k, (i, g) in enumerate(top[:10], 1):
        r = rows[i]
        f.write(f"| {k} | {float(r['lidar_m']):.1f} m | {float(r['ndom_m']):.1f} m | {float(r['lat']):.5f}, {float(r['lon']):.5f} "
                f"([OSM](https://www.openstreetmap.org/?mlat={r['lat']}&mlon={r['lon']}#map=18/{r['lat']}/{r['lon']})) | {r['jahre']} |\n")
print(f"{len(rows)} Spitzen, {len(np.unique(lab))} Gruppen ab 50 m, davon {len(bad)} als Bauwerk/Leitung verworfen; "
      f"{len(cand)} gewertete Laser-Spitzen")
print(open(f"{out}/bw_top10.md").read())
