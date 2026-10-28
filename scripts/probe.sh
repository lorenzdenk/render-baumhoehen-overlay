#!/usr/bin/env bash
# Erkundet das Open-GeoData-Portal des LGL (URL-Schema für DGM/DOM-Downloads).
set -x
mkdir -p p && cd p
curl -sSL https://opengeodata.lgl-bw.de/ -o index.html
head -c 3000 index.html; echo
for js in $(grep -oE '(src|href)="[^"]+"' index.html | sed -E 's/.*="([^"]+)"/\1/' | grep -E '\.(js|json)$'); do
  u="$js"; case "$u" in http*) ;; *) u="https://opengeodata.lgl-bw.de/${u#/}";; esac
  curl -sSL "$u" -o "$(basename "$u")" || true
done
ls -la
set +x
echo "=== strings with dgm/dom"
grep -ohE '"[^"]{0,150}(dgm|dom|DGM|DOM)[^"]{0,150}"' *.js 2>/dev/null | sort -u | head -150
echo "=== urls"
grep -ohE 'https?://[a-zA-Z0-9_./{}$-]+' *.js | sort -u | head -100
echo "=== /data/ refs"
grep -ohE '.{150}/data/.{150}' *.js | head -20
echo "=== json refs"
grep -ohE '"[a-zA-Z0-9_/.-]*\.(json|geojson)"' *.js | sort -u | head -50
echo "=== candidates"
for u in https://opengeodata.lgl-bw.de/data/ https://opengeodata.lgl-bw.de/data/dgm/ https://opengeodata.lgl-bw.de/data/dom1/ https://opengeodata.lgl-bw.de/data/dom/; do
  echo "== $u"; curl -sSI "$u" | head -3; curl -sSL "$u" | head -c 1500; echo
done
echo "=== qgis plugin"
cd ..; curl -sSL -o plugin.zip "https://plugins.qgis.org/plugins/BW_OpenGeodataDownloader/version/latest/download/" || true
file plugin.zip; unzip -o -q plugin.zip -d plugin || true
grep -rhoE 'https?://[^"'"'"' )]+' plugin | sort -u | head -80
grep -rnE 'dgm|dom|DGM|DOM|zip' plugin --include=*.py | cut -c1-250 | head -120
