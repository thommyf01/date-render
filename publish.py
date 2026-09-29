#!/usr/bin/env python3
"""publish.py - kopieer de render-output naar de site-checkout en werk locations.json bij.

Draait in de workflow NA render_city.py. De site (website-git) staat uitgecheckt in ./site.
Upsert: de bestaande steden blijven staan, deze stad wordt toegevoegd/vervangen.
"""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SITE = HERE / "site" / "portfolio"


def main():
    slug = json.loads((HERE / "payload.json").read_text(encoding="utf-8"))["slug"]
    meta = json.loads((OUT / f"{slug}.meta.json").read_text(encoding="utf-8"))

    models = SITE / "assets" / "models"
    renders = SITE / "assets" / "renders"
    models.mkdir(parents=True, exist_ok=True)
    renders.mkdir(parents=True, exist_ok=True)

    shutil.copy2(OUT / f"{slug}.glb", models / f"{slug}.glb")
    shutil.copy2(OUT / f"{slug}-large.webp", renders / f"{slug}-large.webp")
    shutil.copy2(OUT / f"{slug}-thumb.webp", renders / f"{slug}-thumb.webp")

    locpath = SITE / "data" / "locations.json"
    try:
        locs = json.loads(locpath.read_text(encoding="utf-8"))
        if not isinstance(locs, list):
            locs = []
    except Exception:
        locs = []

    entry = {
        "id": meta["id"], "city": meta["city"],
        "country": meta.get("country"),
        "src": meta["src"], "thumb": meta["thumb"],
        "width": meta["width"], "height": meta["height"],
    }
    locs = [l for l in locs if l.get("id") != slug] + [entry]
    locpath.parent.mkdir(parents=True, exist_ok=True)
    locpath.write_text(json.dumps(locs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"gepubliceerd: {slug} ({len(locs)} steden in locations.json)")


if __name__ == "__main__":
    main()
