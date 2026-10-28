#!/usr/bin/env bash
# Erkundung 3: Kachelparität LGL, Inhalt einer nDOM-Kachel, Tasmanien-Quellen.
sudo apt-get install -y -qq gdal-bin >/dev/null 2>&1
B=https://opengeodata.lgl-bw.de
echo "=== parity"
for p in ndom1:ndom1 dom1:dom1 dgm:dgm1; do d=${p%%:*}; f=${p##*:}
  for e in 512 513; do for n in 5278 5279; do
    u="$B/data/$d/${f}_32_${e}_${n}_2_bw.zip"; c=$(curl -s -o /dev/null -w '%{http_code} %{size_download}' -r 0-0 "$u"); echo "$u -> $c"
  done; done
done
echo "=== one ndom1 tile"
for e in 512 513; do for n in 5278 5279; do
  u="$B/data/ndom1/ndom1_32_${e}_${n}_2_bw.zip"
  if curl -sf -o t.zip "$u"; then echo "got $u"; unzip -l t.zip; unzip -o -q t.zip -d t; for f in $(find t -name '*.tif' | head -1); do gdalinfo -stats "$f" | head -60; done; break 2; fi
done; done
echo "=== one dgm1 tile listing"
for e in 512 513; do for n in 5278 5279; do
  u="$B/data/dgm/dgm1_32_${e}_${n}_2_bw.zip"
  if curl -sf -o g.zip "$u"; then echo "got $u"; unzip -l g.zip | head; break 2; fi
done; done
echo "=== TheList other folders"
for f in FPA Utilities AerialPhotoViewer HT NCH; do echo "== $f"; curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/$f?f=json" | python3 -c "import sys,json; d=json.load(sys.stdin); print([s['name']+':'+s['type'] for s in d.get('services',[])], d.get('folders'))"; done
for s in $(for f in FPA Utilities AerialPhotoViewer; do curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/$f?f=json" | python3 -c "import sys,json; [print(x['name']+'/'+x['type']) for x in json.load(sys.stdin).get('services',[])]"; done); do
  echo "== $s"; curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/$s/layers?f=json" | python3 -c "import sys,json
d=json.load(sys.stdin)
for l in d.get('layers',[]): print(' ', l['id'], l['type'], l['name'])" 2>&1 | head -40
done
echo "=== listdata opendata"
curl -sSL https://listdata.thelist.tas.gov.au/opendata/ | grep -oiE 'href="[^"]*(canopy|height|chm|lidar|dsm|dem)[^"]*"' | head -40
curl -sSL https://listdata.thelist.tas.gov.au/opendata/ | grep -oE 'href="[^"]+"' | head -80
echo "=== Meta canopy height (AWS)"
aws s3 ls --no-sign-request s3://dataforgood-fb-data/forests/v1/ 2>&1 | head
aws s3 ls --no-sign-request s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/ 2>&1 | head
aws s3 ls --no-sign-request s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/chm/ 2>&1 | head -5
aws s3 ls --no-sign-request s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/chm/ 2>&1 | wc -l
