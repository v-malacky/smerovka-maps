"""Builds the offline search database (places, streets, POIs, addresses, speed cameras)
from OSM PBF extracts.

Usage: python build_search.py <out.db> <file1.osm.pbf> [file2.osm.pbf ...]

Tables (read by the app, see SearchDb.kt):
  entry(id INTEGER PRIMARY KEY, kind INT, name TEXT, city TEXT, cat TEXT, lat INT, lon INT, rank INT)
  entry_fts  FTS4 (contentless) over normalized "name city", docid = entry.id
  addr(street TEXT, num TEXT, lat INT, lon INT)     -- street = norm(addr:street or addr:place)
  camera(lat INT, lon INT, maxspeed INT, direction TEXT)
Coordinates are stored as int E6.
"""
import math
import os
import re
import sqlite3
import sys
import unicodedata

import osmium

KIND_PLACE, KIND_STREET, KIND_POI = 0, 1, 2
PLACE_WEIGHT = {"city": 8.0, "town": 3.0, "village": 1.0, "suburb": 0.6, "hamlet": 0.4}
PLACE_RANK = {"city": 100, "town": 80, "village": 60, "suburb": 50, "hamlet": 40, "locality": 20, "isolated_dwelling": 10}

POI_AMENITY = {
    "fuel": "Čerpacia stanica", "charging_station": "Nabíjacia stanica", "parking": "Parkovisko",
    "restaurant": "Reštaurácia", "cafe": "Kaviareň", "fast_food": "Rýchle občerstvenie", "pub": "Krčma",
    "bar": "Bar", "hospital": "Nemocnica", "clinic": "Poliklinika", "pharmacy": "Lekáreň",
    "police": "Polícia", "bank": "Banka", "atm": "Bankomat", "school": "Škola", "university": "Univerzita",
    "cinema": "Kino", "theatre": "Divadlo", "townhall": "Obecný úrad", "post_office": "Pošta",
    "car_wash": "Autoumyváreň", "car_rental": "Požičovňa áut", "kindergarten": "Škôlka",
    "doctors": "Lekár", "dentist": "Zubár", "veterinary": "Veterinár", "place_of_worship": "Kostol",
    "marketplace": "Trhovisko", "library": "Knižnica", "bus_station": "Autobusová stanica",
}
POI_TOURISM = {
    "hotel": "Hotel", "motel": "Motel", "guest_house": "Penzión", "hostel": "Hostel", "apartment": "Apartmán",
    "attraction": "Atrakcia", "museum": "Múzeum", "viewpoint": "Vyhliadka", "camp_site": "Kemping",
    "zoo": "ZOO", "theme_park": "Zábavný park", "information": None,
}
POI_LEISURE = {"park": "Park", "sports_centre": "Športové centrum", "water_park": "Aquapark", "stadium": "Štadión",
               "swimming_pool": "Kúpalisko", "golf_course": "Golf"}
POI_OTHER = {
    ("railway", "station"): "Železničná stanica", ("aeroway", "aerodrome"): "Letisko",
    ("historic", "castle"): "Hrad", ("historic", "ruins"): "Zrúcanina", ("natural", "peak"): "Vrchol",
    ("highway", "rest_area"): "Odpočívadlo", ("highway", "services"): "Odpočívadlo", ("shop", "car_repair"): "Autoservis",
    ("craft", "car_repair"): "Autoservis", ("shop", "mall"): "Nákupné centrum", ("shop", "supermarket"): "Supermarket",
    ("amenity", "border_control"): "Hraničný priechod", ("barrier", "border_control"): "Hraničný priechod",
}

MAJOR_POI = {"Nákupné centrum", "Nemocnica", "Letisko", "Železničná stanica", "Autobusová stanica", "Univerzita",
             "ZOO", "Aquapark", "Štadión", "Hrad", "Hraničný priechod", "Zábavný park"}

NONALNUM = re.compile(r"[^0-9a-z]+")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ß", "ss").replace("ł", "l").replace("đ", "d").replace("ø", "o")
    return NONALNUM.sub(" ", s).strip()


def e6(v):
    return int(round(v * 1e6))


class Grid:
    """Simple spatial hash of places, used to give streets / POIs a city name."""

    def __init__(self, cell=0.1):
        self.cell, self.g = cell, {}

    def add(self, lat, lon, name, kind):
        k = (int(lat / self.cell), int(lon / self.cell))
        self.g.setdefault(k, []).append((lat, lon, name, PLACE_WEIGHT.get(kind, 0.3)))

    def nearest(self, lat, lon):
        ci, cj = int(lat / self.cell), int(lon / self.cell)
        best, bd = None, 1e18
        coslat = math.cos(math.radians(lat))
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (a, b, name, w) in self.g.get((ci + di, cj + dj), ()):
                    d = math.hypot(a - lat, (b - lon) * coslat) / w
                    if d < bd:
                        bd, best = d, name
        return best


def poi_category(tags):
    # hrady a zámky bývajú zároveň tourism=attraction – kategória "Hrad" je pre vyhľadávanie dôležitejšia
    if tags.get("historic") == "castle":
        return "Hrad"
    a = tags.get("amenity")
    if a in POI_AMENITY:
        return POI_AMENITY[a]
    t = tags.get("tourism")
    if t in POI_TOURISM and POI_TOURISM[t]:
        return POI_TOURISM[t]
    lz = tags.get("leisure")
    if lz in POI_LEISURE:
        return POI_LEISURE[lz]
    for (k, v), c in POI_OTHER.items():
        if tags.get(k) == v:
            return c
    if "shop" in tags:
        return "Obchod"
    return None


def centroid(way):
    lat = lon = 0.0
    n = 0
    for nd in way.nodes:
        if nd.location.valid():
            lat += nd.location.lat
            lon += nd.location.lon
            n += 1
    return (lat / n, lon / n) if n else None


def middle(way):
    nodes = [nd for nd in way.nodes if nd.location.valid()]
    if not nodes:
        return None
    m = nodes[len(nodes) // 2]
    return m.location.lat, m.location.lon


ROAD_TYPES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
              "living_street", "pedestrian", "service", "motorway_link", "trunk_link", "primary_link",
              "secondary_link", "tertiary_link", "road", "track"}


def main():
    out = sys.argv[1]
    files = sys.argv[2:]
    if os.path.exists(out):
        os.remove(out)

    places = {}       # (norm name, cell) -> row
    grid = Grid()
    streets_raw = []  # (name, lat, lon)
    pois = []         # (name, cat, lat, lon)
    addrs = set()     # (street_norm, num, lat_e6, lon_e6)
    cameras = []
    extras = []

    # Pass 1: places (needed for street -> city assignment).
    for f in files:
        print("places", f, flush=True)
        for o in osmium.FileProcessor(f, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("place")):
            kind = o.tags.get("place")
            name = o.tags.get("name")
            if kind not in PLACE_RANK or not name or not o.location.valid():
                continue
            lat, lon = o.location.lat, o.location.lon
            key = (norm(name), kind, round(lat, 2), round(lon, 2))
            pop = o.tags.get("population", "0")
            try:
                popn = int(re.sub(r"\D", "", pop) or 0)
            except ValueError:
                popn = 0
            places[key] = (name, kind, lat, lon, popn)
            if kind in PLACE_WEIGHT:
                grid.add(lat, lon, name, kind)

    def add_addr(tags, lat, lon):
        has_street = "addr:street" in tags
        street = tags.get("addr:street") or tags.get("addr:place")
        if not street:
            return
        sn = norm(street)
        nums = set()
        # SK/CZ style "súpisné/orientačné": with a street people type the orientačné číslo,
        # in villages without streets (addr:place) the súpisné číslo.
        extra = tags.get("addr:streetnumber" if has_street else "addr:conscriptionnumber")
        for v in (tags.get("addr:housenumber"), extra):
            if not v:
                continue
            for part in re.split(r"[;,]", v):
                p = part.strip().lower().replace(" ", "")
                if not p:
                    continue
                if "/" in p:
                    a, _, b = p.partition("/")
                    p = (b if has_street else a) or p
                nums.add(p)
        for n in nums:
            addrs.add((sn, n, e6(lat), e6(lon)))

    # Pass 2: everything else.
    keys = osmium.filter.KeyFilter("highway", "addr:housenumber", "addr:streetnumber", "addr:conscriptionnumber",
                                   "amenity", "shop", "tourism", "leisure", "railway", "aeroway", "historic",
                                   "natural", "craft", "barrier")
    for f in files:
        print("objects", f, flush=True)
        fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(keys)
        for o in fp:
            tags = o.tags
            if o.is_node():
                if not o.location.valid():
                    continue
                lat, lon = o.location.lat, o.location.lon
                if tags.get("highway") == "speed_camera":
                    ms = re.sub(r"\D", "", tags.get("maxspeed", "") or "")
                    cameras.append((e6(lat), e6(lon), int(ms) if ms else 0, tags.get("direction", "")))
                    continue
            else:
                hw = tags.get("highway")
                if hw in ROAD_TYPES and "name" in tags:
                    m = middle(o)
                    if m:
                        streets_raw.append((tags["name"], m[0], m[1]))
                if not any(k in tags for k in ("addr:housenumber", "addr:streetnumber", "addr:conscriptionnumber",
                                               "amenity", "shop", "tourism", "leisure", "railway", "aeroway",
                                               "historic", "craft")):
                    continue
                c = centroid(o)
                if not c:
                    continue
                lat, lon = c
            add_addr(tags, lat, lon)
            cat = poi_category(tags)
            if cat:
                name = tags.get("name") or tags.get("brand")
                if name:
                    pois.append((name, cat, lat, lon))
                # doplnkové info pre navigáciu: značka čerpačky, poplatok za parkovanie
                if cat in ("Čerpacia stanica", "Parkovisko", "Nabíjacia stanica", "Odpočívadlo"):
                    brand = tags.get("brand") or tags.get("operator") or ""
                    fee = (tags.get("fee") or "").lower()
                    fee = "no" if fee in ("no", "free") else ("yes" if fee and fee not in ("unknown",) else "")
                    if brand or fee:
                        extras.append((e6(lat), e6(lon), cat, brand[:40], fee))

    # Pass 2b: POI zakreslené ako multipolygón (veľké nákupné centrá, nemocnice, areály – napr. "Galleria
    # Eurovea", "Bory Mall"). Bod = priemer uzlov vonkajších ciest.
    rel_pois = {}   # rel id -> (name, cat, tags dict, [outer way ids])
    want_ways = set()
    for f in files:
        print("relations", f, flush=True)
        for o in osmium.FileProcessor(f, osmium.osm.RELATION).with_filter(osmium.filter.TagFilter(("type", "multipolygon"))):
            tags = o.tags
            cat = poi_category(tags)
            name = tags.get("name") or tags.get("brand")
            has_addr = "addr:housenumber" in tags or "addr:conscriptionnumber" in tags
            if not (cat and name) and not has_addr:
                continue
            outer = [m.ref for m in o.members if m.type == "w" and m.role in ("outer", "")]
            if not outer:
                continue
            rel_pois[o.id] = (name, cat, {t.k: t.v for t in tags}, outer)
            want_ways.update(outer)
    if want_ways:
        way_sum = {}
        for f in files:
            fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY).with_locations() \
                .with_filter(osmium.filter.IdFilter(list(want_ways)))
            for w in fp:
                if not w.is_way() or w.id not in want_ways:
                    continue
                la = lo = 0.0
                n = 0
                for nd in w.nodes:
                    if nd.location.valid():
                        la += nd.location.lat
                        lo += nd.location.lon
                        n += 1
                if n:
                    way_sum[w.id] = (la, lo, n)
        for (name, cat, tags, outer) in rel_pois.values():
            parts = [way_sum[w] for w in outer if w in way_sum]
            n = sum(p[2] for p in parts)
            if not n:
                continue
            lat = sum(p[0] for p in parts) / n
            lon = sum(p[1] for p in parts) / n
            add_addr(tags, lat, lon)
            if cat and name:
                pois.append((name, cat, lat, lon))
        print(f"multipolygon POI: {len(rel_pois)}", flush=True)

    # Pass 3: radary zadané cez relácie type=enforcement (rola "device") – nie vždy majú highway=speed_camera
    known = {(c[0], c[1]) for c in cameras}
    devices = {}
    for f in files:
        for o in osmium.FileProcessor(f, osmium.osm.RELATION).with_filter(osmium.filter.TagFilter(("type", "enforcement"))):
            if o.tags.get("enforcement") not in ("maxspeed", "average_speed", "mobile_speed_camera", None):
                continue
            ms = re.sub(r"\D", "", o.tags.get("maxspeed", "") or "")
            for m in o.members:
                if m.role == "device" and m.type == "n":
                    devices[m.ref] = int(ms) if ms else 0
    if devices:
        for f in files:
            for o in osmium.FileProcessor(f, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(list(devices))):
                if not o.location.valid():
                    continue
                key = (e6(o.location.lat), e6(o.location.lon))
                if key not in known:
                    known.add(key)
                    cameras.append((key[0], key[1], devices.get(o.id, 0), o.tags.get("direction", "")))

    # Pass 4: dopravné značky STOP a "daj prednosť" (highway=stop / give_way). Smer, z ktorého platia:
    # pri direction=forward/backward sa dopočíta z cesty (smer príjazdu k uzlu), inak -1 (neznámy).
    sign_nodes = {}
    for f in files:
        for o in osmium.FileProcessor(f, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("highway")):
            hw = o.tags.get("highway")
            if hw not in ("stop", "give_way") or not o.location.valid():
                continue
            d = (o.tags.get("direction") or "").strip().lower()
            sign_nodes[o.id] = [e6(o.location.lat), e6(o.location.lon), 0 if hw == "stop" else 1, d, -1]
    # železničné priecestia pre autá (railway=level_crossing; railway=crossing je len pre chodcov) – kind 2
    for f in files:
        for o in osmium.FileProcessor(f, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("railway")):
            if o.tags.get("railway") != "level_crossing" or not o.location.valid():
                continue
            sign_nodes[o.id] = [e6(o.location.lat), e6(o.location.lon), 2, "", -1]
    want = {nid for nid, v in sign_nodes.items() if v[3] in ("forward", "backward")}
    if want:
        for f in files:
            fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY).with_locations()                 .with_filter(osmium.filter.KeyFilter("highway"))
            for w in fp:
                if not w.is_way():
                    continue
                nodes = [n for n in w.nodes]
                for i, n in enumerate(nodes):
                    if n.ref not in want:
                        continue
                    v = sign_nodes[n.ref]
                    # forward = platí pre jazdu v smere cesty: prichádza sa od predošlého uzla
                    j = i - 1 if v[3] == "forward" else i + 1
                    if 0 <= j < len(nodes) and nodes[j].location.valid() and n.location.valid():
                        a, b = nodes[j].location, n.location
                        dx = (b.lon - a.lon) * math.cos(math.radians(b.lat))
                        dy = b.lat - a.lat
                        v[4] = int(round(math.degrees(math.atan2(dx, dy)))) % 360
    signs = [(v[0], v[1], v[2], v[4]) for v in sign_nodes.values()]

    print(f"places={len(places)} streets_raw={len(streets_raw)} pois={len(pois)} addrs={len(addrs)} "
          f"cameras={len(cameras)}", flush=True)

    db = sqlite3.connect(out)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=4096;
        CREATE TABLE entry(id INTEGER PRIMARY KEY, kind INT, name TEXT, city TEXT, cat TEXT, lat INT, lon INT, rank INT);
        CREATE VIRTUAL TABLE entry_fts USING fts4(content="", text, prefix="2,3,4");
        CREATE TABLE addr(street TEXT, num TEXT, lat INT, lon INT, PRIMARY KEY(street, num, lat, lon)) WITHOUT ROWID;
        CREATE TABLE camera(lat INT, lon INT, maxspeed INT, direction TEXT);
        CREATE TABLE sign(lat INT, lon INT, kind INT, bearing INT);
        CREATE TABLE poi_extra(lat INT, lon INT, cat TEXT, brand TEXT, fee TEXT);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    rows = []
    nid = 0

    def emit(kind, name, city, cat, lat, lon, rank):
        nonlocal nid
        nid += 1
        rows.append((nid, kind, name, city, cat, e6(lat), e6(lon), rank))

    for (name, kind, lat, lon, pop) in places.values():
        emit(KIND_PLACE, name, None, kind, lat, lon, PLACE_RANK[kind] + min(pop // 20000, 50))

    # Streets: one entry per (name, city).
    seen = {}
    for (name, lat, lon) in streets_raw:
        city = grid.nearest(lat, lon) or ""
        k = (norm(name), city)
        if k not in seen:
            seen[k] = [name, city, lat, lon, 1]
        else:
            s = seen[k]
            s[2] += lat
            s[3] += lon
            s[4] += 1
    for (name, city, lat, lon, n) in seen.values():
        emit(KIND_STREET, name, city, None, lat / n, lon / n, 30)

    pseen = set()
    for (name, cat, lat, lon) in pois:
        k = (norm(name), cat, round(lat, 3), round(lon, 3))
        if k in pseen:
            continue
        pseen.add(k)
        # významné ciele (nákupné centrum, nemocnica, letisko…) pred rovnomennou kaviarňou či obchodom
        emit(KIND_POI, name, grid.nearest(lat, lon) or "", cat, lat, lon, 35 if cat in MAJOR_POI else 20)

    db.executemany("INSERT INTO entry VALUES (?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO entry_fts(docid, text) VALUES (?,?)",
                   ((r[0], norm(r[2]) + " " + norm(r[3] or "") + " " + norm(r[4] or "")) for r in rows))
    db.executemany("INSERT INTO addr VALUES (?,?,?,?)", sorted(addrs))
    db.executemany("INSERT INTO camera VALUES (?,?,?,?)", cameras)
    db.executemany("INSERT INTO sign VALUES (?,?,?,?)", signs)
    db.executemany("INSERT INTO poi_extra VALUES (?,?,?,?,?)", extras)
    db.execute("INSERT INTO entry_fts(entry_fts) VALUES ('optimize')")
    db.execute("INSERT INTO meta VALUES ('version', '1')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"entries={len(rows)} size={os.path.getsize(out) / 1e6:.0f} MB", flush=True)


main()
