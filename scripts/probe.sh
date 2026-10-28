#!/usr/bin/env bash
# Erkundung: Produktkonfiguration des LGL-Portals und TheList-Dienste (Tasmanien).
mkdir -p p && cd p
B=https://opengeodata.lgl-bw.de
for f in assets/config/local/odp-products.json assets/environment/config.json; do
  curl -sSL "$B/$f" -o "$(basename $f)"; echo "=== $f ($(wc -c < $(basename $f)) bytes)"
done
cp odp-products.json ../logs/ 2>/dev/null; cp config.json ../logs/ 2>/dev/null
curl -sSL $B/main-326ADFDD.js -o main.js; curl -sSL $B/chunk-V4BVW554.js -o c1.js; curl -sSL $B/chunk-7C3R5VC3.js -o c2.js; curl -sSL $B/chunk-PKDEYTYO.js -o c3.js
echo "=== js: zip/download/data context"
grep -ohE '.{200}(\.zip|/data/|downloadUrl|download_url|fileName).{200}' main.js c1.js c2.js c3.js | head -30
echo "=== TheList canopy"
for s in Public/NaturalEnvironment Public/NaturalEnvironmentV2 Public/TopographyAndRelief Public/NVAdata Public/OpenDataWFS; do
  echo "== $s"; curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/$s/MapServer/layers?f=json" \
    | python3 -c "import sys,json
d=json.load(sys.stdin)
for l in d.get('layers',[]): print(l['id'], l['type'], l['name'], (l.get('description') or '')[:200].replace('\n',' '))" 2>&1 | head -120
done
echo "== services root folders"
for f in Basemaps Raster Public; do curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/$f?f=json" | python3 -c "import sys,json; print([s['name']+':'+s['type'] for s in json.load(sys.stdin)['services']])"; done
