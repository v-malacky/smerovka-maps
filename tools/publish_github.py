"""Nahrá mapové balíky (data/packages) ako GitHub Release – appka si ich potom sťahuje z internetu.

Použitie:
    set GITHUB_TOKEN=ghp_...            (token s právom "Contents: Read and write" pre repozitár)
    python tools/publish_github.py <vlastník>/<repozitár>

- Každé zostavenie máp (catalog.json → "build") = jeden release s tagom maps-<build>.
- Súbory sa nahrajú ako <krajina>-<súbor> (napr. sk-map.pmtiles); katalóg má absolútne URL na tento tag,
  takže rozbehnuté sťahovanie sa nepokazí, keď medzitým vyjde nová verzia.
- Release je označený ako "latest" → appka číta
  https://github.com/<vlastník>/<repozitár>/releases/latest/download/catalog.json
- Opätovné spustenie preskočí už nahraté súbory (rovnaká veľkosť), takže sa dá pokračovať po prerušení.
- Súbory, ktoré sa od minulého zverejnenia nezmenili (rovnaká veľkosť a neboli prepísané), sa nenahrávajú
  znova – katalóg na ne odkazuje v staršom release a má pri nich "build" verzie, v ktorej vznikli; appka
  ich pri aktualizácii nesťahuje. Staršie release, na ktoré katalóg ešte odkazuje, sa nemažú.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "packages")
API = "https://api.github.com"


def req(method, url, token, data=None, headers=None, raw=False):
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "map-publisher",
    }
    h.update(headers or {})
    body = data
    if data is not None and not raw:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            txt = resp.read()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        raise SystemExit(f"{method} {url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")


class Progress:
    """Súbor ako stream pre urllib s výpisom priebehu."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)
        self.done = 0
        self.t0 = time.time()
        self.last = 0

    def read(self, n=-1):
        b = self.f.read(1 << 20 if n is None or n < 0 else n)
        self.done += len(b)
        if time.time() - self.last > 2 or not b:
            self.last = time.time()
            mb = self.done / 1e6
            speed = mb / max(time.time() - self.t0, 0.1)
            print(f"\r    {mb:8.1f} / {self.size / 1e6:.1f} MB  ({speed:.1f} MB/s)", end="", flush=True)
        return b

    def __len__(self):
        return self.size


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        raise SystemExit(__doc__)
    repo = sys.argv[1]
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Chýba GITHUB_TOKEN (pozri návod v hlavičke skriptu).")

    catalog = json.load(open(os.path.join(ROOT, "catalog.json"), encoding="utf-8"))
    build = catalog["build"]
    # minulé zverejnenie: nezmenené súbory sa len prevezmú (adresa + verzia)
    prev_path = os.path.join(ROOT, "catalog.cloud.json")
    prev, prev_time = {}, 0.0
    if os.path.exists(prev_path):
        pc = json.load(open(prev_path, encoding="utf-8"))
        prev_time = os.path.getmtime(prev_path)
        for p in pc.get("packages", []):
            for f in p.get("files", []):
                if f.get("url"):
                    prev[(p["id"], f["name"])] = dict(f, build=f.get("build") or pc["build"])
    tag = f"maps-{build}"

    rel = req("GET", f"{API}/repos/{repo}/releases/tags/{tag}", token)
    if rel is None:
        rel = req("POST", f"{API}/repos/{repo}/releases", token, {
            "tag_name": tag,
            "name": f"Mapy {build}",
            "body": "Offline mapy, trasy a vyhľadávanie (OpenStreetMap) pre navigáciu.",
            "make_latest": "true",
        })
        print(f"Vytvorený release {tag}")
    existing = {a["name"]: a for a in rel.get("assets", [])}
    upload_base = rel["upload_url"].split("{")[0]
    download_base = f"https://github.com/{repo}/releases/download/{tag}/"

    def upload(path, name, content_type="application/octet-stream", force=False):
        size = os.path.getsize(path)
        a = existing.get(name)
        if a and not force and a["size"] == size and a["state"] == "uploaded":
            print(f"  {name}: už nahraté")
            return
        if a:  # neúplný/iný – zmazať a nahrať znova
            req("DELETE", f"{API}/repos/{repo}/releases/assets/{a['id']}", token)
        print(f"  {name} ({size / 1e6:.0f} MB)")
        for attempt in range(1, 4):
            try:
                req("POST", f"{upload_base}?name={name}", token, Progress(path), raw=True,
                    headers={"Content-Type": content_type, "Content-Length": str(size)})
                print()
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                print(f"\n    chyba ({e}), skúšam znova {attempt}/3")
                time.sleep(5 * attempt)
                fresh = req("GET", f"{API}/repos/{repo}/releases/{rel['id']}", token)
                for x in fresh.get("assets", []):
                    if x["name"] == name:
                        req("DELETE", f"{API}/repos/{repo}/releases/assets/{x['id']}", token)
        raise SystemExit(f"Nahrávanie {name} zlyhalo")

    for pkg in catalog["packages"]:
        print(f"{pkg['name']} ({pkg['id']})")
        for f in pkg["files"]:
            asset = f"{pkg['id']}-{f['name']}"
            path = os.path.join(ROOT, pkg["id"], f["name"])
            f["size"] = os.path.getsize(path)
            old = prev.get((pkg["id"], f["name"]))
            if old and old["size"] == f["size"] and os.path.getmtime(path) < prev_time and old["build"] != build:
                print(f"  {asset}: nezmenené (verzia {old['build']})")
                f["url"], f["build"] = old["url"], old["build"]
                continue
            upload(path, asset)
            f["url"] = download_base + asset
            f["build"] = build

    # katalóg až nakoniec – appka uvidí novú verziu, až keď sú nahraté všetky súbory
    out = os.path.join(ROOT, "catalog.cloud.json")
    json.dump(catalog, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    upload(out, "catalog.json", "application/json", force=True)
    req("PATCH", f"{API}/repos/{repo}/releases/{rel['id']}", token, {"make_latest": "true"})
    # staré verzie upratať (ponechať posledné 2 – rozbehnuté sťahovania z predošlej verzie dobehnú)
    keep = int(os.environ.get("KEEP_RELEASES", "2"))
    rels = [r for r in (req("GET", f"{API}/repos/{repo}/releases?per_page=50", token) or [])
            if r["tag_name"].startswith("maps-")]
    rels.sort(key=lambda r: r["tag_name"], reverse=True)
    used = {f["url"].split("/releases/download/")[1].split("/")[0]
            for p in catalog["packages"] for f in p["files"] if "/releases/download/" in f.get("url", "")}
    for old in [r for r in rels[keep:] if r["tag_name"] not in used]:
        print(f"Mažem starú verziu {old['tag_name']}")
        req("DELETE", f"{API}/repos/{repo}/releases/{old['id']}", token)
        req("DELETE", f"{API}/repos/{repo}/git/refs/tags/{old['tag_name']}", token)
    print(f"\nHotovo. Adresa pre appku:\n  https://github.com/{repo}/releases/latest/download/catalog.json")


if __name__ == "__main__":
    main()
