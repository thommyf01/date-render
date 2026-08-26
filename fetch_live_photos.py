#!/usr/bin/env python3
"""fetch_live_photos.py - haal de HUIDIGE live-foto's op van Cloudflare.

Zo kan de CI de complete site deployen (foto's + nieuwe render) zonder dat de foto's
in git staan. De foto's blijven dus 'handmatig' (jouw build.py + deploy.ps1); de CI
spiegelt alleen wat er nu live staat. Draait in de workflow; de site staat in ./site.

Veiligheid: mislukt meer dan 10% van de downloads, dan stopt het script met een fout
(de workflow deployt dan NIET, zodat je nooit foto's van de live site kwijtraakt).
"""
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE / "site" / "portfolio"
BASE = os.environ.get("SITE_BASE", "https://thom-fotografie.pages.dev").rstrip("/")


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "date-render"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main():
    try:
        pj = get(f"{BASE}/data/photos.js").decode("utf-8", "replace")
    except Exception as e:
        sys.exit(f"photos.js niet op te halen ({e}) - deploy afgebroken (foto's zouden verdwijnen).")

    (SITE / "data").mkdir(parents=True, exist_ok=True)
    (SITE / "data" / "photos.js").write_text(pj, encoding="utf-8")

    m = re.search(r"window\.PHOTOS\s*=\s*(\[.*\]);?\s*$", pj, re.S)
    if not m:
        sys.exit("PHOTOS niet te parsen uit photos.js - deploy afgebroken.")
    photos = json.loads(m.group(1))

    paths = [p[k] for p in photos for k in ("src", "thumb") if p.get(k)]
    print(f"{len(paths)} fotobestanden ophalen van {BASE} ...", flush=True)

    def fetch_one(rel):
        dest = SITE / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(get(f"{BASE}/{rel}"))
            return True
        except Exception as e:
            print(f"  mislukt: {rel} ({e})")
            return False

    with ThreadPoolExecutor(max_workers=16) as ex:
        ok = sum(1 for r in ex.map(fetch_one, paths) if r)

    print(f"klaar: {ok}/{len(paths)} foto's opgehaald", flush=True)
    if paths and ok < len(paths) * 0.9:
        sys.exit("Te veel foto's mislukt - deploy afgebroken om verlies te voorkomen.")


if __name__ == "__main__":
    main()
