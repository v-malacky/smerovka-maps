"""Merges several (sorted) OSM PBF extracts into one sorted PBF without duplicates.

Geofabrik country extracts overlap along borders; Valhalla fails on the duplicates
("invalid vector subscript") when given multiple files, so we k-way merge by id.

Usage: python merge_pbf.py <out.osm.pbf> <in1.osm.pbf> [in2 ...]
"""
import heapq
import os
import sys
import time

import osmium

out, files = sys.argv[1], sys.argv[2:]
if os.path.exists(out):
    os.remove(out)

writer = osmium.SimpleWriter(out)
t0 = time.time()
for kind, add in ((osmium.osm.NODE, writer.add_node), (osmium.osm.WAY, writer.add_way),
                  (osmium.osm.RELATION, writer.add_relation)):
    streams = [iter(osmium.FileProcessor(f, kind)) for f in files]
    last = None
    n = 0
    for o in heapq.merge(*streams, key=lambda x: x.id):
        if o.id == last:
            continue
        last = o.id
        add(o)
        n += 1
        if n % 20_000_000 == 0:
            print(f"  {kind} {n / 1e6:.0f}M ({time.time() - t0:.0f}s)", flush=True)
    print(f"{kind}: {n} objects ({time.time() - t0:.0f}s)", flush=True)
writer.close()
print(f"done {os.path.getsize(out) / 1e6:.0f} MB", flush=True)
