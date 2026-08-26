"""
export_osm.py — OSM data → JSON voor Blender-render (alle steden)
=================================================================
Per stad: gebouwen+hoogte, water, groen, bomen en datum-locaties ophalen,
projecteren naar lokale meters en wegschrijven als scene_<slug>.json.
Centrum = zwaartepunt van de datum-locaties; straal dekt alle plekken + marge.

Gebruik: python export_osm.py [slug]      (geen slug = alle steden)
"""

import os, sys, json, math, warnings
import requests
import numpy as np
import osmnx as ox
from shapely.geometry import Polygon, MultiPolygon, Point, LineString
from pyproj import Transformer

# Realistische dakhoogtes uit de 3D BAG (NL). BAG=1 zet het aan; werkt alleen voor
# NL-steden (Kraków heeft geen BAG). Zie functie fetch_bag_heights.
BAG = os.environ.get("BAG", "0") == "1"
_TO_RD = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
BAG_URL = "https://api.3dbag.nl/collections/pand/items"
BAG_CRS = "http://www.opengis.net/def/crs/EPSG/0/7415"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

MARGIN_M = int(os.environ.get("MARGIN_M", 450))  # extra context rond de buitenste plek; env-override om vlakke randen te vullen
MIN_R    = 650     # minimale straal (1 plek)
MAX_R    = int(os.environ.get("MAX_R", 3200))   # cap (anders te zware render); env-override voor brede overzichten

# Steden: slug → lijst (naam, lat, lon[, icon])
CITIES = {
    "delft": [
        ("Moeke", 52.01173, 4.36234),
        ("Heilige Geestkerkhof", 52.01256, 4.35582),
        ("Doelenplein", 52.01443, 4.35891),
    ],
    "rotterdam": [
        ("Schiedamse Vest", 51.91598, 4.47980),
        ("Pannekoekstraat", 51.92390, 4.48687),
        ("Mooie Boules", 51.92921, 4.47724),
        # ("Hollywood Café", 51.89620, 4.52508),  # overgeslagen (3,7 km buiten cluster) — zie docs/OVERGESLAGEN_LOCATIES.md
        ("Stalles", 51.91700, 4.47203),
        ("Bergweg", 51.93211, 4.47098),
    ],
    "denhaag": [
        ("Beachclub La Parade", 52.07268, 4.22291),
    ],
    "utrecht": [
        ("Pathé Rembrandt", 52.09392, 5.11629),
        ("Feest der Muzen", 52.09322, 5.12686),
        ("Malieblad", 52.08896, 5.12992),
    ],
    "enschede": [
        ("Drienerbeeklaan", 52.24071, 6.84965),
        # ("Weerseloseweg", 52.26499, 6.87648),  # overgeslagen — zie docs/OVERGESLAGEN_LOCATIES.md
        ("Enschede (nieuw)", 52.25188113667364, 6.877168805437961),  # toegevoegd; naam nog bevestigen
        ("J.J. van Deinselaan", 52.20480, 6.90106),
        ("Gronausestraat", 52.21881, 6.91980),
        ("Van der Poel", 52.22240, 6.89302),
    ],
    "balice": [
        ("Kraków Airport", 50.07341, 19.80192, "airplane"),
    ],
}

# 'Alles'-varianten = de DEFINITIEVE bron voor Rotterdam/Enschede: breed stads-
# overzicht met álle plekken in beeld (incl. eerder overgeslagen). Exporteer met
# ruime marge zodat de randen met stad vullen, en render met FIT_ALL=1:
#   MARGIN_M=1400 MAX_R=5500 python export_osm.py rotterdam_all
#   PRESET=3 FIT_ALL=1 BRIDGE=1 RESX=2480 RESY=3508 blender ... scene_rotterdam_all.json renders/render_rotterdam_A2.png
CITIES["rotterdam_all"] = CITIES["rotterdam"] + [
    ("Hollywood Café", 51.89620, 4.52508)]
CITIES["enschede_all"] = CITIES["enschede"] + [
    ("Weerseloseweg", 52.26499, 6.87648)]

# Framing-ankers (lat, lon): extra punten die wél in beeld moeten vallen maar GEEN
# marker krijgen. Sturen het centrum, de fetch-straal én de zoom. Gebruikt om
# Utrecht uit te zoomen zodat Utrecht Centraal (station) net meekomt.
ANCHORS = {
    "utrecht": [(52.08940, 5.11000)],   # Utrecht Centraal
}


def to_local_m(lat, lon, clat, clon):
    x = (lon - clon) * 111_320 * math.cos(math.radians(clat))
    y = (lat - clat) * 111_320
    return x, y


def building_height_m(row, default_levels=3):
    val = row.get("height")
    if val is not None and str(val) not in ("nan", "None", ""):
        try:
            return float(str(val).replace("m", "").split(";")[0].strip())
        except ValueError:
            pass
    for col in ("building:levels", "levels"):
        val = row.get(col)
        if val is not None and str(val) not in ("nan", "None", ""):
            try:
                return float(str(val).split(";")[0].strip()) * 3.0
            except ValueError:
                pass
    return default_levels * 3.0


def collect_rings(gdf, clat, clon):
    out = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        polys = ([geom] if isinstance(geom, Polygon)
                 else list(geom.geoms) if isinstance(geom, MultiPolygon) else [])
        for poly in polys:
            if poly.is_valid and not poly.is_empty:
                ring = [to_local_m(la, lo, clat, clon)
                        for lo, la in poly.exterior.coords]
                if len(ring) >= 4:
                    out.append(ring)
    return out


def buffered_rings_local(ln, clat, clon, width_m):
    """Projecteer een lijn naar lokale meters en buffer in METERS (niet graden!).
    De oude code bufferde lon/lat-lijnen rechtstreeks → 6 'graden' ≈ 666 km en
    corrupte reuzeringen. Geeft lokale-meter-ringen terug (meestal 1)."""
    local = LineString([to_local_m(la, lo, clat, clon) for lo, la in ln.coords])
    poly = local.buffer(width_m)
    polys = ([poly] if poly.geom_type == "Polygon"
             else list(poly.geoms) if poly.geom_type == "MultiPolygon" else [])
    out = []
    for p in polys:
        if p.is_valid and not p.is_empty:
            ring = list(p.exterior.coords)
            if len(ring) >= 4:
                out.append(ring)
    return out


def fetch(clat, clon, radius, tags):
    try:
        return ox.features_from_point((clat, clon), dist=radius, tags=tags)
    except Exception as e:
        print(f"    geen data {tags}: {e}")
        import geopandas as gpd
        return gpd.GeoDataFrame()


def fetch_bag_heights(clat, clon, radius):
    """Haal per-pand dakhoogtes uit de 3D BAG (CityJSON) in een bbox rond
    (clat,clon). Geeft (centroids_rd Nx2, heights N) terug voor matching.
    Hoogte = b3_h_dak_70p - b3_h_maaiveld (m boven maaiveld)."""
    cx, cy = _TO_RD.transform(clon, clat)
    bbox = f"{cx-radius},{cy-radius},{cx+radius},{cy+radius}"
    cxs, cys, hs = [], [], []
    params = {"bbox": bbox, "bbox-crs": BAG_CRS, "limit": 1000}
    url, pages = BAG_URL, 0
    while url and pages < 600:
        try:
            r = requests.get(url, params=params, timeout=90)
            d = r.json()
        except Exception as e:
            print(f"    BAG-fetch gestopt: {e}")
            break
        tr = d.get("metadata", {}).get("transform")
        if not tr:
            break
        sx, sy, _ = tr["scale"]; tx, ty, _ = tr["translate"]
        for f in d.get("features", []):
            v = f["vertices"]
            for obj in f["CityObjects"].values():
                a = obj.get("attributes", {})
                hd, hm = a.get("b3_h_dak_70p"), a.get("b3_h_maaiveld")
                if hd is None or hm is None:
                    continue
                g0 = next((g for g in obj.get("geometry", [])
                           if str(g.get("lod")) in ("0", "0.0")), None)
                if not g0:
                    continue
                idx = [i for surf in g0["boundaries"] for ring in surf for i in ring]
                if not idx:
                    continue
                cxs.append(sum(v[i][0] for i in idx) / len(idx) * sx + tx)
                cys.append(sum(v[i][1] for i in idx) / len(idx) * sy + ty)
                hs.append(max(2.0, hd - hm))
        url = next((l["href"] for l in d.get("links", []) if l.get("rel") == "next"), None)
        params = None
        pages += 1
    if not hs:
        return None, None
    return np.column_stack([cxs, cys]), np.array(hs)


def export_city(slug, spots):
    # dedupe (zelfde coord dubbel in cords.txt)
    seen, uniq = set(), []
    for s in spots:
        key = (round(s[1], 5), round(s[2], 5))
        if key not in seen:
            seen.add(key); uniq.append(s)
    spots = uniq

    anchors = ANCHORS.get(slug, [])
    pts = [(s[1], s[2]) for s in spots] + list(anchors)   # plekken + framing-ankers
    clat = sum(p[0] for p in pts) / len(pts)
    clon = sum(p[1] for p in pts) / len(pts)
    spread = max((math.hypot(*to_local_m(la, lo, clat, clon))
                  for la, lo in pts), default=0)
    radius = int(min(MAX_R, max(MIN_R, spread + MARGIN_M)))
    print(f"\n[{slug}] {len(spots)} plekken, centrum {clat:.4f},{clon:.4f}, "
          f"straal {radius}m")

    print("  gebouwen...", end="", flush=True)
    bgdf = fetch(clat, clon, radius, {"building": True})
    buildings = []
    for _, row in bgdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        polys = ([geom] if isinstance(geom, Polygon)
                 else list(geom.geoms) if isinstance(geom, MultiPolygon) else [])
        h = building_height_m(row)
        # Landmark: alleen echte bezienswaardigheden (kerken/torens/monumenten),
        # NIET op hoogte (anders wordt de hele hoogbouw-skyline goud).
        def _has(col):
            v = row.get(col)
            return v is not None and str(v) not in ("nan", "None", "")
        lm = (_has("historic") or _has("tourism") or
              str(row.get("amenity")) == "place_of_worship" or
              str(row.get("man_made")) == "tower" or
              str(row.get("building")) in ("church", "cathedral", "chapel"))
        for poly in polys:
            if poly.is_valid and not poly.is_empty:
                ring = [to_local_m(la, lo, clat, clon)
                        for lo, la in poly.exterior.coords]
                if len(ring) >= 4:
                    b = {"ring": ring, "h": h}
                    if lm:
                        b["lm"] = True
                    c = poly.centroid
                    b["_rd"] = _TO_RD.transform(c.x, c.y)   # voor BAG-matching
                    buildings.append(b)
    n_lm = sum(1 for b in buildings if b.get("lm"))
    print(f" {len(buildings)} ({n_lm} landmarks)")

    # Realistische dakhoogtes uit 3D BAG (NL): match elk OSM-gebouw op de
    # dichtstbijzijnde BAG-pand-centroïde (≤12 m) en neem die hoogte over.
    if BAG and slug != "balice" and buildings:
        print("  3D BAG hoogtes...", end="", flush=True)
        cents, heights = fetch_bag_heights(clat, clon, radius)
        if heights is not None and len(heights):
            from scipy.spatial import cKDTree
            tree = cKDTree(cents)
            q = np.array([b["_rd"] for b in buildings])
            dist, idx = tree.query(q, k=1)
            n_match = 0
            for b, di, ii in zip(buildings, dist, idx):
                if di <= 12.0:
                    b["h"] = float(heights[ii]); n_match += 1
            print(f" {n_match}/{len(buildings)} gematcht ({len(heights)} BAG-panden)")
        else:
            print(" geen BAG-data")
    for b in buildings:
        b.pop("_rd", None)

    print("  water/groen/bomen...", end="", flush=True)
    water = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius, {"natural": "water", "waterway": True}),
        clat, clon)]
    green = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius, {
            "leisure": ["park", "garden", "playground"],
            "landuse": ["grass", "recreation_ground", "meadow", "village_green"],
            "natural": ["scrub", "grassland"]}), clat, clon)]
    trees = []
    for _, row in fetch(clat, clon, radius, {"natural": "tree"}).iterrows():
        g = row.geometry
        if isinstance(g, Point):
            trees.append(list(to_local_m(g.y, g.x, clat, clon)))
    sand = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius, {"natural": ["beach", "sand", "dune"]}),
        clat, clon)]

    # Extra data-lagen
    forest = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius, {"landuse": "forest", "natural": "wood"}),
        clat, clon)]
    pitch = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius, {"leisure": ["pitch", "sports_centre",
                                               "stadium", "track"]}), clat, clon)]
    rail = []
    rg = fetch(clat, clon, radius,
               {"railway": ["rail", "light_rail", "tram", "subway"]})
    for _, row in rg.iterrows():
        g = row.geometry
        if g is None or g.is_empty:
            continue
        lines = ([g] if g.geom_type == "LineString"
                 else list(g.geoms) if g.geom_type == "MultiLineString" else [])
        for ln in lines:
            rail.extend(buffered_rings_local(ln, clat, clon, 2.5))
    # Bruggen (vlakken + gebufferde brug-wegen)
    bridge = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius, {"man_made": "bridge"}), clat, clon)]
    bg = fetch(clat, clon, radius, {"bridge": ["yes", "viaduct"]})
    for _, row in bg.iterrows():
        g = row.geometry
        if g is None or g.is_empty:
            continue
        if g.geom_type in ("LineString", "MultiLineString"):
            lines = [g] if g.geom_type == "LineString" else list(g.geoms)
            for ln in lines:
                for ring in buffered_rings_local(ln, clat, clon, 6.0):
                    bridge.append({"ring": ring})

    # Havens / kades / pieren
    pier = [{"ring": r} for r in collect_rings(
        fetch(clat, clon, radius,
              {"man_made": ["pier", "quay", "breakwater"],
               "waterway": "dock"}), clat, clon)]

    # Paden (alleen kustlocaties; gebufferde voet-/fietspaden om over de zee in
    # grijs te renderen i.p.v. blauw). Anders leeg.
    path = []
    if slug in ("denhaag",):
        pgd = fetch(clat, clon, radius,
                    {"highway": ["footway", "path", "pedestrian", "service",
                                 "track", "cycleway", "steps"]})
        for _, row in pgd.iterrows():
            g = row.geometry
            if g is None or g.is_empty:
                continue
            lines = ([g] if g.geom_type == "LineString"
                     else list(g.geoms) if g.geom_type == "MultiLineString" else [])
            for ln in lines:
                for ring in buffered_rings_local(ln, clat, clon, 2.0):
                    path.append({"ring": ring})

    print(f" w{len(water)}/g{len(green)}/b{len(trees)}/z{len(sand)}/"
          f"bos{len(forest)}/sport{len(pitch)}/spoor{len(rail)}/"
          f"brug{len(bridge)}/kade{len(pier)}")

    dates = []
    for s in spots:
        x, y = to_local_m(s[1], s[2], clat, clon)
        d = {"name": s[0], "x": x, "y": y}
        if len(s) > 3:
            d["icon"] = s[3]
        dates.append(d)

    anchor_xy = [dict(zip(("x", "y"), to_local_m(la, lo, clat, clon)))
                 for la, lo in anchors]

    scene = {"slug": slug, "center": [clat, clon], "radius_m": radius,
             "buildings": buildings, "water": water, "green": green,
             "trees": trees, "sand": sand, "forest": forest, "pitch": pitch,
             "rail": [{"ring": r} for r in rail], "bridge": bridge,
             "pier": pier, "path": path, "dates": dates, "anchors": anchor_xy}
    ddir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(ddir, exist_ok=True)
    out = os.path.join(ddir, f"scene_{slug}.json")
    with open(out, "w") as f:
        json.dump(scene, f)
    print(f"  → data/scene_{slug}.json ({os.path.getsize(out)//1024} KB)")


if __name__ == "__main__":
    want = sys.argv[1] if len(sys.argv) > 1 else None
    for slug, spots in CITIES.items():
        if want and slug != want:
            continue
        export_city(slug, spots)
    print("\nKlaar.")
