#!/usr/bin/env bash
# Zostavenie všetkých mapových balíkov na Linuxe (GitHub Actions v repozitári smerovka-maps).
# Výstup: data/packages/{<id>/…, catalog.json} – nahrá ho tools/publish_github.py.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data/pbf data/valhalla data/out data/packages
PY=python3
COUNTRIES="slovakia hungary romania austria croatia slovenia czech-republic"

echo "== sťahujem OSM extrakty (Geofabrik)"
for c in $COUNTRIES; do
  curl -sSL --fail --retry 5 -o data/pbf/$c-latest.osm.pbf https://download.geofabrik.de/europe/$c-latest.osm.pbf
done
ls -la data/pbf

echo "== spájam (bez duplicít na hraniciach)"
$PY tools/merge_pbf.py data/pbf/merged.osm.pbf $(for c in $COUNTRIES; do echo data/pbf/$c-latest.osm.pbf; done)

echo "== Valhalla dlaždice"
cd data
valhalla_build_config \
  --mjolnir-tile-dir valhalla/tiles \
  --mjolnir-tile-extract out/valhalla_tiles.tar \
  --mjolnir-admin valhalla/admins.sqlite \
  --mjolnir-concurrency "$(nproc)" > valhalla/valhalla.json
valhalla_build_admins -c valhalla/valhalla.json pbf/merged.osm.pbf
valhalla_build_tiles -c valhalla/valhalla.json pbf/merged.osm.pbf
rm -f pbf/merged.osm.pbf   # miesto na disku
cd "$ROOT"

echo "== mapa (Protomaps – najnovšie zostavenie sveta)"
PLANET_URL=""
for i in $(seq 0 10); do
  d=$(date -u -d "-$i day" +%Y%m%d)
  if curl -sfI "https://build.protomaps.com/$d.pmtiles" > /dev/null; then PLANET_URL="https://build.protomaps.com/$d.pmtiles"; break; fi
done
[ -n "$PLANET_URL" ] || { echo "Protomaps build nenájdený"; exit 1; }
echo "   $PLANET_URL"

echo "== balíky po krajinách"
# všetky mapy sa vystrihnú priamo zo sveta (REGION_MAP prázdny cez ALL_FROM_PLANET)
ALL_FROM_PLANET=1 PLANET_URL="$PLANET_URL" $PY tools/build_packages.py
du -sh data/packages/*
