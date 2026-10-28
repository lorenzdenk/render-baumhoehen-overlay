#!/usr/bin/env python3
"""Baumgruppen: Bäume ab --min m (Standard 80) mit höchstens --link m Abstand (Kette, Standard 250 m) bilden
eine Gruppe. Schreibt alle Gruppen als CSV (eine Zeile je Gruppe) und GeoJSON (alle Bäume).
usage: tree_groups.py <trees.csv> <out prefix> [--min 80] [--link 250]"""
import argparse
import csv
import json
import math

import numpy as np
from scipy.spatial import cKDTree

ap = argparse.ArgumentParser()
ap.add_argument("trees"); ap.add_argument("out")
ap.add_argument("--min", type=float, default=80); ap.add_argument("--link", type=float, default=250)
a = ap.parse_args()
rows = [r for r in csv.DictReader(open(a.trees)) if float(r["height_m"]) >= a.min]   # nur Bäume ab --min
lon = np.array([float(r["lon"]) for r in rows]); lat = np.array([float(r["lat"]) for r in rows])
h = np.array([float(r["height_m"]) for r in rows])
lat0 = math.radians(float(np.median(lat)))
xy = np.c_[np.radians(lon) * 6371000 * math.cos(lat0), np.radians(lat) * 6371000]
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zielscheibe import clusters
lab = clusters(xy, a.link)
srt = np.argsort(lab, kind="stable")
groups = {i: m for i, m in enumerate(np.split(srt, np.nonzero(np.diff(lab[srt]))[0] + 1))}
res = []
for m in groups.values():
    m = np.array(m)
    top = m[np.argmax(h[m])]
    if h[top] >= a.min:
        res.append(dict(hoechster_m=float(h[top]), lat=float(lat[top]), lon=float(lon[top]), baeume_in_gruppe=len(m),
                        baeume_ab_90m=int((h[m] >= 90).sum()),
                        ausdehnung_m=round(float(np.ptp(xy[m], axis=0).max()), 0),
                        baeume_ab_80m_liste=[(float(lat[i]), float(lon[i]), float(h[i])) for i in m[np.argsort(-h[m])] if h[i] >= a.min]))
res.sort(key=lambda g: -g["hoechster_m"])
keys = ["gruppe", "hoechster_m", "lat", "lon", "baeume_in_gruppe", "baeume_ab_90m", "ausdehnung_m"]
with open(a.out + ".csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(keys)
    for k, g in enumerate(res, 1):
        w.writerow([k] + [round(g[x], 6) if isinstance(g[x], float) else g[x] for x in keys[1:]])
feats = []
for k, g in enumerate(res, 1):
    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(g["lon"], 6), round(g["lat"], 6)]},
                  "properties": {"gruppe": k, "art": "höchster Baum", **{x: g[x] for x in keys[1:] if x not in ("lat", "lon")}}})
    for la, lo, hh in g["baeume_ab_80m_liste"][1:]:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lo, 6), round(la, 6)]},
                      "properties": {"gruppe": k, "art": f"Baum ab {a.min:g} m", "hoehe_m": hh}})
json.dump({"type": "FeatureCollection", "features": feats}, open(a.out + ".geojson", "w"), ensure_ascii=False)
print(f"{len(rows)} Bäume >= {a.min:g} m in {len(res)} Gruppen")
for g in res[:15]:
    print(f"  {g['hoechster_m']:.1f} m  {g['lat']:.5f}, {g['lon']:.5f}  ({g['baeume_in_gruppe']} Bäume >= {a.min:g} m, davon {g['baeume_ab_90m']} >= 90 m)")
