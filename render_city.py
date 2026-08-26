#!/usr/bin/env python3
"""
render_city.py - render 1 stad voor de Date-pagina, aangestuurd door een payload.

Leest {slug, city, spots:[{label,lat,lon,icon?}]} uit --payload <json> (of env
PAYLOAD_FILE) en produceert in out/:
  <slug>.glb          3D-model voor de web-viewer
  <slug>-large.webp   poster (grote versie / laad-fallback)
  <slug>-thumb.webp   poster-thumbnail voor de tegel
  <slug>.meta.json    {id, city, src, thumb, width, height}  (voor locations.json)

Stappen:
  1) export_osm.export_city(slug, spots) -> data/scene_<slug>.json   (osmnx)
  2) Blender export_glb.py                -> out/<slug>.glb
  3) Blender render_blender.py (PRESET=3) -> out/render_<slug>.png
  4) Pillow: png -> webp (large + thumb)
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
BLENDER = os.environ.get("BLENDER", "blender")  # CI: op PATH; lokaal: volledig pad
THUMB_MAX, LARGE_MAX = 900, 1800
THUMB_Q, LARGE_Q = 80, 82


def run_blender(script, scene, out, defaults=None):
    # defaults vullen alleen gaten; per-stad renderOpts (al in os.environ) winnen.
    env = dict(os.environ)
    for k, v in (defaults or {}).items():
        env.setdefault(k, v)
    cmd = [BLENDER, "-b", "-P", str(HERE / script), "--", str(scene), str(out)]
    print("  >", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, env=env, cwd=str(HERE))
    if r.returncode != 0:
        raise SystemExit(f"Blender faalde ({script}): exit {r.returncode}")


def save_webp(img, dest, max_edge, q):
    im = img.copy()
    im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "WEBP", quality=q, method=6)
    return im.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", help="JSON-bestand met {slug, city, spots}")
    args = ap.parse_args()
    payload_path = args.payload or os.environ.get("PAYLOAD_FILE")
    if not payload_path:
        raise SystemExit("Geen payload (--payload of env PAYLOAD_FILE).")
    data = json.loads(Path(payload_path).read_text(encoding="utf-8"))

    slug = data["slug"]
    city = data.get("city") or slug
    spots = []
    for s in (data.get("spots") or []):
        t = [s.get("label") or city, float(s["lat"]), float(s["lon"])]
        if s.get("icon"):
            t.append(s["icon"])
        spots.append(tuple(t))
    if not spots:
        raise SystemExit("Payload heeft geen geldige spots.")

    out = HERE / "out"
    out.mkdir(exist_ok=True)

    # Per-stad render-instellingen (framing/look) toepassen als env VOOR de import,
    # zodat export_osm (MARGIN_M/MAX_R/BAG) en de Blender-runs (FIT_ALL/BRIDGE/...) ze zien.
    # Zo houdt een re-render dezelfde uitsnede als het origineel.
    for k, v in (data.get("renderOpts") or {}).items():
        os.environ[str(k)] = str(v)

    # BAG-dakhoogtes aan vóór de import (export_osm leest BAG bij het laden).
    # Buiten NL geeft de BAG-API simpelweg geen data -> valt terug op OSM-levels.
    os.environ.setdefault("BAG", "1")
    import export_osm

    print(f"[1/4] OSM-data ophalen ({slug}, {len(spots)} plekken)...", flush=True)
    export_osm.export_city(slug, spots)
    scene = HERE / "data" / f"scene_{slug}.json"
    if not scene.exists():
        raise SystemExit(f"scene niet gegenereerd: {scene}")

    print("[2/4] .glb exporteren (Blender)...", flush=True)
    glb = out / f"{slug}.glb"
    run_blender("export_glb.py", scene, glb, {"GREEN": "1", "TREES": "1", "FOREST": "1"})
    if not glb.exists():
        raise SystemExit(".glb niet gemaakt")

    print("[3/4] poster renderen (Blender)...", flush=True)
    png = out / f"render_{slug}.png"
    run_blender("render_blender.py", scene, png,
                {"PRESET": "3", "RESX": os.environ.get("RESX", "1500"),
                 "RESY": os.environ.get("RESY", "2000"),
                 "SAMPLES": os.environ.get("SAMPLES", "24")})
    if not png.exists():
        raise SystemExit("poster-render niet gemaakt")

    print("[4/4] webp maken...", flush=True)
    with Image.open(png) as im:
        im = im.convert("RGB")
        w, h = save_webp(im, out / f"{slug}-large.webp", LARGE_MAX, LARGE_Q)
        save_webp(im, out / f"{slug}-thumb.webp", THUMB_MAX, THUMB_Q)

    meta = {
        "id": slug, "city": city,
        "src": f"assets/renders/{slug}-large.webp",
        "thumb": f"assets/renders/{slug}-thumb.webp",
        "width": w, "height": h,
    }
    (out / f"{slug}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    print("META " + json.dumps(meta), flush=True)
    print("Klaar.", flush=True)


if __name__ == "__main__":
    main()
