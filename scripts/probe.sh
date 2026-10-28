#!/usr/bin/env bash
# Erkundung Tasmanien: Download der TheList-Kronenhöhe und Meta-CHM am Styx (-42.9669, 146.7402).
sudo apt-get install -y -qq gdal-bin >/dev/null 2>&1
echo "=== listdata opendata index"
curl -sSL https://listdata.thelist.tas.gov.au/opendata/ -o od.html; wc -c od.html
grep -oiE '[^"'"'"' <>]*(canopy|Canopy|CANOPY)[^"'"'"' <>]*' od.html | sort -u | head
for s in $(grep -oE 'src="[^"]+\.js"' od.html | sed -E 's/src="([^"]+)"/\1/'); do
  case "$s" in http*|//*) continue;; esac
  curl -sSL "https://listdata.thelist.tas.gov.au/opendata/$s" -o "js_$(basename $s)"; echo "js $s $(wc -c < js_$(basename $s))"
done
grep -ohiE '.{100}canopy.{200}' js_* od.html 2>/dev/null | head -10
grep -ohE '"[^"]*\.(json|xml)"' js_* od.html 2>/dev/null | sort -u | head -20
for f in data/opendata.json opendata.json resources/opendata.json data/index.json; do echo "== $f"; curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' "https://listdata.thelist.tas.gov.au/opendata/$f"; done
echo "=== candidate canopy URLs"
for u in \
  https://listdata.thelist.tas.gov.au/opendata/data/LIST_TREE_CANOPY_HEIGHT_STATEWIDE.zip \
  https://listdata.thelist.tas.gov.au/opendata/data/LIST_Tree_Canopy_Height.zip \
  https://listdata.thelist.tas.gov.au/opendata/data/LIST_TREE_CANOPY_HEIGHT.zip \
  ; do echo "$u -> $(curl -sS -o /dev/null -I -w '%{http_code} %{size_download}' "$u") $(curl -sSI "$u" | grep -i content-length)"; done
echo "=== RasterMisc service"
curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/Raster/RasterMisc/MapServer?f=json" | python3 -c "import sys,json; d=json.load(sys.stdin); print({k:d.get(k) for k in ('capabilities','singleFusedMapCache','supportsDynamicLayers','maxImageHeight','maxImageWidth','supportedImageFormatTypes','copyrightText')}); print([ (l['id'],l['name']) for l in d['layers']])"
echo "== identify at the point"
curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/Raster/RasterMisc/MapServer/identify?geometry=146.740223,-42.966869&geometryType=esriGeometryPoint&sr=4326&layers=all:0&tolerance=1&mapExtent=146.7,-43.0,146.8,-42.9&imageDisplay=400,400,96&returnGeometry=false&f=json" | head -c 1500; echo
echo "== export as float tiff?"
curl -sSL -o exp.tif "https://services.thelist.tas.gov.au/arcgis/rest/services/Raster/RasterMisc/MapServer/export?bbox=146.73,-42.975,146.75,-42.96&bboxSR=4326&imageSR=3857&size=600,600&layers=show:0&format=tiff&transparent=false&f=image"
file exp.tif; gdalinfo -stats exp.tif 2>&1 | grep -E 'Band|Type|Min|Max|Mean|Size is' | head
curl -sSL -o exp.png "https://services.thelist.tas.gov.au/arcgis/rest/services/Raster/RasterMisc/MapServer/export?bbox=146.73,-42.975,146.75,-42.96&bboxSR=4326&imageSR=3857&size=600,600&layers=show:0&format=png32&transparent=true&f=image"
file exp.png; cp exp.png logs/ 2>/dev/null; cp exp.tif logs/ 2>/dev/null
curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/Raster/RasterMisc/MapServer/0?f=json" | head -c 3000; echo
curl -sSL "https://services.thelist.tas.gov.au/arcgis/rest/services/Raster/RasterMisc/MapServer/legend?f=json" | head -c 3000; echo
echo "=== Meta CHM tile for point"
python3 - <<'PY'
import math
lat, lon = -42.966869, 146.740223
for z in (9,):
    n = 2**z; x = int((lon+180)/360*n); y = int((1-math.log(math.tan(math.radians(lat))+1/math.cos(math.radians(lat)))/math.pi)/2*n)
    q = ''.join(str(((x>>i)&1) + 2*((y>>i)&1)) for i in range(z-1,-1,-1))
    print("quadkey", z, q); open('qk','w').write(q)
PY
q=$(cat qk); aws s3 ls --no-sign-request s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/chm/$q.tif
aws s3 cp --no-sign-request s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/chm/$q.tif meta.tif && gdalinfo meta.tif | grep -E 'Size is|Origin|Pixel Size|Type|NoData|EPSG|ID\["EPSG"' | tail -8
gdallocationinfo -valonly -wgs84 meta.tif 146.740223 -42.966869
