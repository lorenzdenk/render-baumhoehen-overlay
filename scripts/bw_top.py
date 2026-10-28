#!/usr/bin/env python3
"""Rangliste der höchsten Bäume aus den Kandidaten von bw_scan.py.
usage: bw_top.py <out dir> cand_*.csv"""
import csv
import math
import sys

out, files = sys.argv[1], sys.argv[2:]
rows = []
for f in files:
    for r in csv.DictReader(open(f)):
        rows.append(r)
ok = [r for r in rows if not r["verworfen"]]
for r in ok:
    r["h"] = float(r["lidar_m"]) if r["lidar_m"] else float(r["ndom_m"])
ok.sort(key=lambda r: -r["h"])
top = []
for r in ok:
    x, y = float(r["x"]), float(r["y"])
    if all(math.hypot(x - float(t["x"]), y - float(t["y"])) > 25 for t in top):
        top.append(r)
    if len(top) >= 50:
        break
keys = ["rang", "lat", "lon", "lidar_m", "ndom_m", "jahre", "crown_px", "forest_frac", "x", "y"]
with open(f"{out}/bw_top50.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(keys)
    for i, r in enumerate(top, 1):
        w.writerow([i] + [r[k] for k in keys[1:]])
with open(f"{out}/bw_top10.md", "w") as f:
    f.write("| Rang | Höhe Laser (DOM1−DGM1) | Höhe nDOM | Koordinaten | Jahr(e) |\n|---|---|---|---|---|\n")
    for i, r in enumerate(top[:10], 1):
        f.write(f"| {i} | {r['lidar_m'] or '–'} m | {r['ndom_m']} m | {float(r['lat']):.5f}, {float(r['lon']):.5f} "
                f"([OSM](https://www.openstreetmap.org/?mlat={r['lat']}&mlon={r['lon']}#map=18/{r['lat']}/{r['lon']})) | {r['jahre']} |\n")
rej = {}
for r in rows:
    rej[r["verworfen"] or "ok"] = rej.get(r["verworfen"] or "ok", 0) + 1
nd = [float(r["ndom_m"]) for r in rows]
print(f"{len(rows)} Spitzen ab 40 m (nDOM), davon {rej}; nDOM-Maximum {max(nd) if nd else 0}, "
      f"Anzahl genau 55,0: {sum(1 for v in nd if abs(v - 55) < 0.01)}")
print(open(f"{out}/bw_top10.md").read())
