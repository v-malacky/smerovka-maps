"""Rozdelí offline dáta na balíky po krajinách + catalog.json pre sťahovanie v aplikácii.

Vstupy (vytvorí build_data.sh + extract_pmtiles.py):
  data/out/map.pmtiles          – mapa celého regiónu
  data/valhalla/tiles/          – Valhalla dlaždice celého regiónu (jeden build => konzistentné na hraniciach)
  data/pbf/<krajina>-latest.osm.pbf
Výstup: data/packages/catalog.json, data/packages/<id>/{map.pmtiles,routing.tar,search.db}

Použitie: python build_packages.py [id ...]     (bez argumentov = všetky)
"""
import datetime
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "..", "data")
PY = sys.executable

COUNTRIES = {
    # id: (názov, geofabrik, bbox lon_min, lat_min, lon_max, lat_max)
    "sk": ("Slovensko", "slovakia", (16.8, 47.7, 22.6, 49.65)),
    "at": ("Rakúsko", "austria", (9.5, 46.35, 17.2, 49.05)),
    "hu": ("Maďarsko", "hungary", (16.1, 45.7, 22.95, 48.6)),
    "ro": ("Rumunsko", "romania", (20.2, 43.6, 29.75, 48.3)),
    "hr": ("Chorvátsko", "croatia", (13.45, 42.35, 19.5, 46.6)),
    "si": ("Slovinsko", "slovenia", (13.35, 45.4, 16.65, 46.9)),
    "cz": ("Česko", "czech-republic", (12.05, 48.5, 18.9, 51.1)),
}
# mapa: data/out/map.pmtiles pokrýva len pôvodný región – ostatné krajiny sa vystrihnú priamo
# z celosvetovej mapy Protomaps (PLANET_URL, napr. https://build.protomaps.com/20261116.pmtiles)
REGION_MAP = {"sk", "at", "hu", "ro", "hr"}
# Valhalla hierarchia: level -> veľkosť dlaždice v stupňoch
LEVELS = {0: 4.0, 1: 1.0, 2: 0.25}


def tile_bbox(level, tile_id):
    size = LEVELS[level]
    ncols = int(360 / size)
    row, col = divmod(tile_id, ncols)
    lon0 = -180 + col * size
    lat0 = -90 + row * size
    return lon0, lat0, lon0 + size, lat0 + size


def intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def routing_tar(bbox, out):
    tiles_dir = os.path.join(DATA, "valhalla", "tiles")
    # okraj 0.3° – aby trasy cez hranicu mali kus susednej siete
    bb = (bbox[0] - 0.3, bbox[1] - 0.3, bbox[2] + 0.3, bbox[3] + 0.3)
    n = 0
    with tarfile.open(out, "w", format=tarfile.USTAR_FORMAT) as tar:
        for lvl in sorted(LEVELS):
            base = os.path.join(tiles_dir, str(lvl))
            for dirpath, _, files in os.walk(base):
                for f in files:
                    if not f.endswith(".gph"):
                        continue
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, tiles_dir).replace("\\", "/")
                    tid = int("".join(rel.split("/")[1:]).replace(".gph", ""))
                    if intersects(tile_bbox(lvl, tid), bb):
                        tar.add(full, arcname=rel)
                        n += 1
    return n


def main():
    ids = sys.argv[1:] or list(COUNTRIES)
    out_root = os.path.join(DATA, "packages")
    os.makedirs(out_root, exist_ok=True)
    build = datetime.date.today().isoformat()
    for cid in ids:
        name, geofabrik, bbox = COUNTRIES[cid]
        d = os.path.join(out_root, cid)
        os.makedirs(d, exist_ok=True)
        print(f"== {cid} {name}", flush=True)
        n = routing_tar(bbox, os.path.join(d, "routing.tar"))
        print(f"   routing: {n} dlaždíc", flush=True)
        if os.environ.get("SKIP_MAP") == "1" and os.path.exists(os.path.join(d, "map.pmtiles")):
            print("   mapa: ponechaná", flush=True)
        else:
            local = cid in REGION_MAP and os.environ.get("ALL_FROM_PLANET") != "1"
            src = os.path.join(DATA, "out", "map.pmtiles") if local else os.environ["PLANET_URL"]
            subprocess.run([PY, os.path.join(ROOT, "extract_pmtiles.py"), src,
                            os.path.join(d, "map.pmtiles"), "14", "--bbox=" + ",".join(map(str, bbox))], check=True)
        subprocess.run([PY, os.path.join(ROOT, "build_search.py"), os.path.join(d, "search.db"),
                        os.path.join(DATA, "pbf", f"{geofabrik}-latest.osm.pbf")], check=True)

    packages = []
    for cid, (name, _, bbox) in COUNTRIES.items():
        d = os.path.join(out_root, cid)
        if not os.path.isdir(d):
            continue
        files = [{"name": f, "size": os.path.getsize(os.path.join(d, f))}
                 for f in ("map.pmtiles", "routing.tar", "search.db") if os.path.exists(os.path.join(d, f))]
        packages.append({"id": cid, "name": name, "bbox": bbox, "files": files})
    catalog = {"build": build, "packages": packages}
    with open(os.path.join(out_root, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    print(json.dumps(catalog, ensure_ascii=False)[:500])


main()
