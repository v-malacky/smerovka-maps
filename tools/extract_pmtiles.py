"""Extract a regional PMTiles file from a remote (HTTP range capable) planet PMTiles build.

Usage: python extract_pmtiles.py <url> <out.pmtiles> <maxzoom> [--dry-run]
Region = union of BBOXES below (lon_min, lat_min, lon_max, lat_max).
"""
import bisect
import gzip
import json
import math
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from pmtiles.tile import (
    Compression,
    deserialize_directory,
    deserialize_header,
    zxy_to_tileid,
)
from pmtiles.writer import Writer

BBOXES = [
    (9.4, 46.3, 17.3, 49.1),    # Rakúsko
    (16.8, 47.6, 22.7, 49.7),   # Slovensko
    (16.0, 45.7, 23.0, 48.65),  # Maďarsko
    (20.2, 43.6, 29.8, 48.3),   # Rumunsko
    (13.4, 42.3, 19.5, 46.6),   # Chorvátsko
]

URL = sys.argv[1]
OUT = sys.argv[2]
MAXZOOM = int(sys.argv[3])
DRY = "--dry-run" in sys.argv
for a in sys.argv:
    if a.startswith("--bbox="):  # napr. --bbox=16.8,47.9,17.9,48.6 (malý testovací výrez)
        BBOXES = [tuple(float(x) for x in a[7:].split(","))]

_local = threading.local()


def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        _local.s = s
    return s


def get_bytes(offset, length):
    if not URL.startswith("http"):  # lokálny súbor
        f = getattr(_local, "f", None)
        if f is None:
            f = _local.f = open(URL, "rb")
        f.seek(offset)
        return f.read(length)
    for attempt in range(6):
        try:
            r = session().get(URL, headers={"Range": f"bytes={offset}-{offset + length - 1}"}, timeout=120)
            r.raise_for_status()
            if len(r.content) != length:
                raise IOError(f"short read {len(r.content)} != {length}")
            return r.content
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt} @{offset}: {e}", flush=True)
    raise IOError("giving up")


def lon2x(lon, n):
    return int((lon + 180.0) / 360.0 * n)


def lat2y(lat, n):
    r = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)


def wanted_ids():
    ids = set()
    for z in range(MAXZOOM + 1):
        n = 1 << z
        for (a, b, c, d) in BBOXES:
            x0, x1 = lon2x(a, n), min(lon2x(c, n), n - 1)
            y0, y1 = lat2y(d, n), min(lat2y(b, n), n - 1)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    ids.add(zxy_to_tileid(z, x, y))
    return sorted(ids)


def main():
    header = deserialize_header(get_bytes(0, 127))
    assert header["internal_compression"] == Compression.GZIP
    meta_raw = get_bytes(header["metadata_offset"], header["metadata_length"])
    metadata = json.loads(gzip.decompress(meta_raw))

    ids = wanted_ids()
    print(f"wanted tiles: {len(ids)}", flush=True)

    # Resolve directory entries level by level.
    root = deserialize_directory(get_bytes(header["root_offset"], header["root_length"]))
    pending = [(root, ids)]
    resolved = {}  # tile_id -> (offset, length)
    while pending:
        nxt_groups = {}  # (leaf_off, leaf_len) -> [ids]
        for entries, tids in pending:
            keys = [e.tile_id for e in entries]
            for t in tids:
                i = bisect.bisect_right(keys, t) - 1
                if i < 0:
                    continue
                e = entries[i]
                if e.run_length == 0:
                    nxt_groups.setdefault((e.offset, e.length), []).append(t)
                elif t < e.tile_id + e.run_length:
                    resolved[t] = (e.offset, e.length)
        if not nxt_groups:
            break
        print(f"  fetching {len(nxt_groups)} leaf directories", flush=True)

        def fetch(k):
            off, ln = k
            return k, deserialize_directory(get_bytes(header["leaf_directory_offset"] + off, ln))

        with ThreadPoolExecutor(16) as ex:
            leaves = dict(ex.map(fetch, list(nxt_groups)))
        pending = [(leaves[k], v) for k, v in nxt_groups.items()]

    uniq = sorted(set(resolved.values()))
    total = sum(l for _, l in uniq)
    print(f"present tiles: {len(resolved)}, unique contents: {len(uniq)}, data: {total / 1e9:.2f} GB", flush=True)
    if DRY:
        return

    # Merge into download chunks.
    GAP, MAXC = 256 * 1024, 16 * 1024 * 1024
    chunks = []
    cs, ce = uniq[0][0], uniq[0][0] + uniq[0][1]
    for off, ln in uniq[1:]:
        if off - ce <= GAP and (off + ln) - cs <= MAXC:
            ce = max(ce, off + ln)
        else:
            chunks.append((cs, ce))
            cs, ce = off, off + ln
    chunks.append((cs, ce))
    dl = sum(e - s for s, e in chunks)
    print(f"chunks: {len(chunks)}, download: {dl / 1e9:.2f} GB", flush=True)

    tmp = tempfile.NamedTemporaryFile(delete=False, dir=os.path.dirname(os.path.abspath(OUT)), suffix=".chunks")
    lock = threading.Lock()
    local = {}  # chunk start -> local offset
    done = [0]

    def dl_chunk(c):
        s, e = c
        data = get_bytes(header["tile_data_offset"] + s, e - s)
        with lock:
            local[s] = tmp.tell()
            tmp.write(data)
            done[0] += len(data)
            if len(local) % 50 == 0:
                print(f"  {done[0] / 1e9:.2f}/{dl / 1e9:.2f} GB", flush=True)

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(dl_chunk, chunks))
    tmp.flush()

    starts = [s for s, _ in chunks]
    with open(tmp.name, "rb") as src, open(OUT, "wb") as f:
        w = Writer(f)
        for t in sorted(resolved):
            off, ln = resolved[t]
            ci = bisect.bisect_right(starts, off) - 1
            src.seek(local[starts[ci]] + (off - starts[ci]))
            w.write_tile(t, src.read(ln))
        a = min(b[0] for b in BBOXES); b_ = min(b[1] for b in BBOXES)
        c = max(b[2] for b in BBOXES); d = max(b[3] for b in BBOXES)
        h = dict(header)
        h.update(
            min_lon_e7=int(a * 1e7), min_lat_e7=int(b_ * 1e7),
            max_lon_e7=int(c * 1e7), max_lat_e7=int(d * 1e7),
            center_zoom=7, center_lon_e7=int(19.5e7), center_lat_e7=int(47e7),
        )
        w.finalize(h, metadata)
    tmp.close()
    os.remove(tmp.name)
    print(f"done: {os.path.getsize(OUT) / 1e9:.2f} GB", flush=True)


main()
