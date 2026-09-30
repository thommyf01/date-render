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


# ── Bergterrein (TERRAIN=1) ──────────────────────────────────────────────
# Hoogtes uit de gratis AWS Terrain Tiles (Terrarium-PNG, wereldwijd, geen key).
# Landbedekking (bos/gras/water/rots...) wordt als kleurtextuur (PNG) bij de scene
# opgeslagen, die Blender op het hoogtenet plakt.
TERRAIN_STEP = float(os.environ.get("TERRAIN_STEP", 20))    # meter per gridcel
TERRAIN_ZOOM = int(os.environ.get("TERRAIN_ZOOM", 13))
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


def _tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _get_tile(url, tries=4):
    for k in range(tries):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "date-render/1.0"})
            r.raise_for_status()
            return r
        except requests.RequestException:
            if k == tries - 1:
                raise


def fetch_dem_grid(clat, clon, half, step):
    """Regelmatig raster (n x n) met hoogtes in m; rij j = y van -half..+half,
    kolom i = x van -half..+half (lokale meters, zelfde assen als de scene)."""
    from PIL import Image
    import io
    n = int(round(2 * half / step)) + 1
    xs = np.linspace(-half, half, n)
    lons = clon + xs / (111_320 * math.cos(math.radians(clat)))
    lats = clat + xs / 111_320
    z = TERRAIN_ZOOM
    tx0, ty1 = _tile_xy(lats[0], lons[0], z)       # zuidwest
    tx1, ty0 = _tile_xy(lats[-1], lons[-1], z)     # noordoost
    x_lo, x_hi = int(math.floor(tx0)) - 1, int(math.floor(tx1)) + 1
    y_lo, y_hi = int(math.floor(ty0)) - 1, int(math.floor(ty1)) + 1
    mosaic = np.zeros(((y_hi - y_lo + 1) * 256, (x_hi - x_lo + 1) * 256), dtype=np.float32)
    for ty in range(y_lo, y_hi + 1):
        for tx in range(x_lo, x_hi + 1):
            r = _get_tile(DEM_URL.format(z=z, x=tx, y=ty))
            a = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"), dtype=np.float32)
            mosaic[(ty - y_lo) * 256:(ty - y_lo + 1) * 256,
                   (tx - x_lo) * 256:(tx - x_lo + 1) * 256] = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
    # bilineair samplen op het raster
    PX = np.array([(_tile_xy(clat, lo, z)[0] - x_lo) * 256 for lo in lons])
    PY = np.array([(_tile_xy(la, clon, z)[1] - y_lo) * 256 for la in lats])
    PXg, PYg = np.meshgrid(PX, PY)     # PYg[j,i] hoort bij lat j
    x0 = np.clip(np.floor(PXg).astype(int), 0, mosaic.shape[1] - 2)
    y0 = np.clip(np.floor(PYg).astype(int), 0, mosaic.shape[0] - 2)
    fx, fy = PXg - x0, PYg - y0
    h = (mosaic[y0, x0] * (1 - fx) * (1 - fy) + mosaic[y0, x0 + 1] * fx * (1 - fy) +
         mosaic[y0 + 1, x0] * (1 - fx) * fy + mosaic[y0 + 1, x0 + 1] * fx * fy)
    return h


def terrain_extent(natural_extent):
    """Uitsnede (m) voor een bergkaart: env TERRAIN_EXTENT, anders 1.6x de natuurlijke
    stadsuitsnede (min 1800, max 4500) zodat een kleine stad niet een enorm landschap krijgt."""
    v = os.environ.get("TERRAIN_EXTENT")
    if v:
        return float(v)
    return min(4500.0, max(1800.0, 1.6 * natural_extent))


def build_terrain(slug, clat, clon, half, ddir, layers, ext=None):
    step = TERRAIN_STEP
    print(f"  terrein: DEM ophalen (half {half:.0f}m, stap {step:.0f}m)...", end="", flush=True)
    h = fetch_dem_grid(clat, clon, half, step)
    if layers.get("sea"):          # kust: zee op 0 m (Terrarium geeft zeebodem-dieptes)
        h = np.maximum(h, 0.0)
    base = float(h.min())
    rel = h - base
    print(f" {h.shape[1]}x{h.shape[0]}, {base:.0f}-{h.max():.0f} m")

    # kleurtextuur van de landbedekking (PIL), lineair -> sRGB voor de PNG
    from PIL import Image, ImageDraw
    px_m = 2.0
    S = int(min(4096, 2 * half / px_m))
    scale = S / (2 * half)

    C = {"ground": (0.85, 0.83, 0.79), "green": (0.42, 0.55, 0.30),
         "forest": (0.24, 0.40, 0.20), "rock": (0.68, 0.65, 0.61),
         "sand": (0.86, 0.76, 0.52), "water": (0.08, 0.42, 0.48),
         "road": (0.86, 0.85, 0.82), "path": (0.72, 0.66, 0.55), "sea": (0.10, 0.34, 0.55)}

    def srgb(c):
        return tuple(int(round(255 * (v ** (1 / 2.2)))) for v in c)

    # Brede OSM-landbedekking (1 Overpass-call over het hele terrein). Aanwezig = leidend voor
    # bos/gras; ontbreekt (offline/leeg) = terugval op een DEM-benadering (boomgrens + ruis).
    import time as _time
    for _try in range(3):    # fetch() slikt fouten en geeft dan leeg terug -> opnieuw proberen
        gdf = fetch(clat, clon, int(half), {
            "landuse": ["forest", "grass", "meadow", "farmland"],
            "natural": ["wood", "scrub", "grassland", "heath"]})
        if len(gdf):
            break
        print(f"    landbedekking leeg (poging {_try + 1}/3)")
        _time.sleep(8)
    osm_forest, osm_green = [], []
    for _, row in gdf.iterrows():
        g = row.geometry
        if g is None or g.is_empty:
            continue
        is_forest = str(row.get("natural")) == "wood" or str(row.get("landuse")) == "forest"
        (osm_forest if is_forest else osm_green).append(g)
    have_osm = len(osm_forest) > 0
    print(f"  OSM-landbedekking: {len(osm_forest)} bos, {len(osm_green)} gras -> "
          f"{'OSM' if have_osm else 'DEM-terugval'}")

    # Basiskleur per pixel uit DEM (werkt zonder OSM-landuse): bos onder de boomgrens en op
    # niet-te-steile grond, weide/alpine erboven, rots op steile hellingen. Bospatronen krijgen
    # laagfrequente ruis zodat het geen egaal vlak wordt; OSM-polygonen komen er later overheen.
    gy, gx = np.gradient(h, step)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    treeline = float(os.environ.get("TREELINE", max(300.0, 2600.0 - 55.0 * (abs(clat) - 30.0))))

    def to_tex(a):   # raster (rij j = y oplopend) -> textuurgrootte, rij 0 = noord
        im_ = Image.fromarray(a.astype(np.float32), mode="F").resize((S, S), Image.BILINEAR)
        return np.asarray(im_)[::-1]
    hh = to_tex(h)
    sl = to_tex(slope)
    seed = sum(ord(ch) for ch in slug)
    noise = np.random.default_rng(seed).random((40, 40)).astype(np.float32)
    noise = np.asarray(Image.fromarray(noise, mode="F").resize((S, S), Image.BICUBIC))
    noise = (noise - noise.min()) / (np.ptp(noise) + 1e-6)
    patch = np.clip((noise - 0.30) / 0.25, 0, 1)
    forest_w = np.clip((treeline - hh) / 150, 0, 1) * np.clip((40 - sl) / 8, 0, 1) * (0.35 + 0.65 * patch)
    forest_w = forest_w * 0      # nooit verzonnen bos: zonder OSM-data blijft het terrein neutraal
    alpine_w = np.clip((hh - (treeline - 150)) / 350, 0, 1)
    rock_w = np.clip((sl - 28) / 14, 0, 1)
    lin = lambda c: np.array(c, dtype=np.float32)
    meadow = 0.5 * lin(C["ground"]) + 0.5 * lin(C["green"])
    colr = meadow[None, None, :] * (1 - alpine_w[..., None]) + lin(C["ground"])[None, None, :] * alpine_w[..., None]
    colr = colr * (1 - forest_w[..., None]) + lin(C["forest"])[None, None, :] * forest_w[..., None]
    colr = colr * (1 - 0.8 * rock_w[..., None]) + lin(C["rock"])[None, None, :] * 0.8 * rock_w[..., None]
    im = Image.fromarray(np.clip(255 * colr ** (1 / 2.2), 0, 255).astype(np.uint8), "RGB")
    dr = ImageDraw.Draw(im)

    def paint(rings, col):
        for ring in rings:
            pts = [((x + half) * scale, (half - y) * scale) for x, y in ring]
            if len(pts) >= 3:
                dr.polygon(pts, fill=srgb(C[col]))

    import geopandas as gpd
    fmask = Image.new("L", (S, S), 0)
    fdr = ImageDraw.Draw(fmask)
    if have_osm:
        if osm_green:
            paint(collect_rings(gpd.GeoDataFrame(geometry=osm_green), clat, clon), "green")
        frings = collect_rings(gpd.GeoDataFrame(geometry=osm_forest), clat, clon)
        paint(frings, "forest")
        for ring in frings:
            pts = [((x + half) * scale, (half - y) * scale) for x, y in ring]
            if len(pts) >= 3:
                fdr.polygon(pts, fill=255)
        fm = np.asarray(fmask) > 0
    else:
        print("  WAARSCHUWING: geen OSM-landbedekking -> geen bos/bomen (liever leeg dan verzonnen)")
        fm = np.zeros((S, S), dtype=bool)
    # geen bomen op/tegen gebouwen: footprints (+~24 m rand) uit het bosmasker halen
    em = Image.fromarray((fm * 255).astype(np.uint8), "L")
    edr = ImageDraw.Draw(em)
    for b in layers.get("buildings", []):
        pts = [((x + half) * scale, (half - y) * scale) for x, y in b["ring"]]
        if len(pts) >= 3:
            edr.polygon(pts, fill=0, outline=0, width=7)
    for key in ("water", "sand", "sea", "shore"):   # geen bomen in meren, zee of op strand
        for it in layers.get(key, []):
            pts = [((x + half) * scale, (half - y) * scale) for x, y in it["ring"]]
            if len(pts) >= 3:
                edr.polygon(pts, fill=0)
    fm = (np.asarray(em) > 0) & (hh > 1.0)     # en niet op zeeniveau
    # Wegen en paden (1 Overpass-call), als lijnen in de textuur; breedte in meters naar pixels.
    if os.environ.get("ROADS", "0") == "1":
        W = {"motorway": 10, "trunk": 10, "primary": 9, "secondary": 8, "tertiary": 7,
             "unclassified": 6, "residential": 6, "living_street": 5, "service": 3.5,
             "track": 3.5, "pedestrian": 4, "footway": 2, "path": 2, "cycleway": 2.5,
             "steps": 2, "bridleway": 2}
        rg = fetch(clat, clon, int(half), {"highway": list(W)})
        nroad = 0
        for _, row in rg.iterrows():
            g = row.geometry
            if g is None or g.is_empty:
                continue
            hw = str(row.get("highway"))
            if hw not in W:
                continue
            lines = ([g] if g.geom_type == "LineString"
                     else list(g.geoms) if g.geom_type == "MultiLineString" else [])
            col = "path" if hw in ("footway", "path", "cycleway", "steps", "bridleway", "track") else "road"
            wpx = max(1, int(round(W[hw] * scale)))
            for ln in lines:
                pts = [((to_local_m(la, lo, clat, clon)[0] + half) * scale,
                        (half - to_local_m(la, lo, clat, clon)[1]) * scale) for lo, la in ln.coords]
                if len(pts) >= 2:
                    dr.line(pts, fill=srgb(C[col]), width=wpx, joint="curve")
                    nroad += 1
        print(f"  wegen/paden: {nroad} lijnen")
    # Kust: automatische zee + zandband (uit de OSM-kustlijn) in de textuur
    paint([it["ring"] for it in layers.get("shore", [])], "sand")
    paint([it["ring"] for it in layers.get("sea", [])], "sea")
    # water en zand (scherp afgebakend, al opgehaald voor de stad) bovenop
    for key in ("sand", "water"):
        paint([it["ring"] for it in layers.get(key, [])], key)

    # Bomen: jitter-raster (TREE_SP m) binnen het bosmasker, niet op steile grond.
    sp = float(os.environ.get("TREE_SP", 55))
    rng = np.random.default_rng(seed)
    lim = min(half, (ext or float(os.environ.get("TERRAIN_EXTENT", 4500))) * 0.75)   # alleen in beeld
    trees = []
    xs_ = np.arange(-lim, lim, sp)
    for x0 in xs_:
        for y0 in xs_:
            x, y = x0 + rng.uniform(0, sp), y0 + rng.uniform(0, sp)
            px_, py_ = int((x + half) * scale), int((half - y) * scale)
            if 0 <= px_ < S and 0 <= py_ < S and fm[py_, px_] and sl[py_, px_] < 35:
                trees.append([round(float(x), 1), round(float(y), 1)])
    print(f"  bomen in bos: {len(trees)}")
    tex = f"cover_{slug}.png"
    im.save(os.path.join(ddir, tex))
    return {"half": half, "step": step, "n": int(h.shape[1]), "base": base,
            "z": np.round(rel, 1).flatten().tolist(), "cover": tex, "trees": trees}


# ── Automatische zee uit de OSM-kustlijn (COASTAL=1) ─────────────────────
# OSM-regel: natural=coastline is zo getekend dat LAND LINKS van de looprichting ligt.
# Dus: zee = de deelvlakken van een groot vierkant die rechts van de lijn liggen.
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]


def fetch_coastline(clat, clon, half_m):
    dlat = half_m / 111_320
    dlon = half_m / (111_320 * math.cos(math.radians(clat)))
    q = (f'[out:json][timeout:60];way["natural"="coastline"]'
         f'({clat-dlat},{clon-dlon},{clat+dlat},{clon+dlon});out geom;')
    for attempt in range(2):
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            r = requests.post(url, data={"data": q}, timeout=45,
                              headers={"User-Agent": "date-render/1.0"})
            r.raise_for_status()
            return [[(n["lat"], n["lon"]) for n in el["geometry"]]
                    for el in r.json().get("elements", []) if el.get("geometry")]
        except Exception as e:
            print(f"    kustlijn poging {attempt+1} mislukt: {type(e).__name__}")
    return _coastline_via_osm_api(clat, clon, half_m)


def _coastline_via_osm_api(clat, clon, half_m, cell=0.012):
    """Terugval als Overpass onbereikbaar is: de gewone OSM-API (max 50k nodes per call,
    dus in kleine cellen). Geeft dezelfde vorm terug als fetch_coastline."""
    dlat = half_m / 111_320
    dlon = half_m / (111_320 * math.cos(math.radians(clat)))
    ways = {}
    la = clat - dlat
    while la < clat + dlat:
        lo = clon - dlon
        while lo < clon + dlon:
            box_ = f"{lo},{la},{min(lo+cell, clon+dlon)},{min(la+cell, clat+dlat)}"
            try:
                r = requests.get("https://api.openstreetmap.org/api/0.6/map.json",
                                 params={"bbox": box_}, timeout=90,
                                 headers={"User-Agent": "date-render/1.0"})
                r.raise_for_status()
                els = r.json()["elements"]
                nodes = {e["id"]: (e["lat"], e["lon"]) for e in els if e["type"] == "node"}
                for e in els:
                    if e["type"] == "way" and e.get("tags", {}).get("natural") == "coastline":
                        pts = [nodes[n] for n in e["nodes"] if n in nodes]
                        if len(pts) >= 2:
                            ways[e["id"]] = pts
            except Exception as ex:
                print(f"    OSM-API cel {box_} mislukt: {type(ex).__name__}")
            lo += cell
        la += cell
    print(f"    kustlijn via OSM-API: {len(ways)} way(s)")
    return list(ways.values()) or None


def build_sea(clat, clon, half_m):
    """Geeft lijst zee-ringen (lokale meters) of [] als er geen kustlijn in beeld is."""
    from shapely.geometry import box
    from shapely.ops import linemerge, unary_union, polygonize
    ways = fetch_coastline(clat, clon, half_m)
    if not ways:
        print("  kustlijn: geen data -> geen zee")
        return []
    lines = [LineString([to_local_m(la, lo, clat, clon) for la, lo in w]) for w in ways]
    merged = unary_union(lines)
    if merged.geom_type != "LineString":      # meerdere stukken -> aaneenrijgen waar mogelijk
        merged = linemerge(merged)
    parts = [merged] if merged.geom_type == "LineString" else list(merged.geoms)
    B = half_m
    frame = box(-B, -B, B, B)
    pieces = list(polygonize(unary_union([frame.boundary] + parts).simplify(0)))
    sea = []
    for pc in pieces:
        pt = pc.representative_point()
        # zee als het punt rechts van de dichtstbijzijnde kustlijn-segmentrichting ligt
        best = min(parts, key=lambda l: l.distance(pt))
        d = best.project(pt)
        a = best.interpolate(max(d - 1.0, 0)); b = best.interpolate(min(d + 1.0, best.length))
        cross = (b.x - a.x) * (pt.y - a.y) - (b.y - a.y) * (pt.x - a.x)
        if cross < 0:                      # rechts van de lijn -> zee
            sea.append(pc)
    rings = [list(g.exterior.coords) for pc in sea
             for g in (pc.geoms if pc.geom_type == "MultiPolygon" else [pc])]
    print(f"  kustlijn: {len(parts)} lijn(en), {len(rings)} zee-vlak(ken)")
    return rings


def build_shore(sea_rings, width_m):
    """Zandstrook: band van width_m langs de zee, aan de landkant (zee-buffer minus zee)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    sea = unary_union([Polygon(r) for r in sea_rings if len(r) >= 4])
    band = sea.buffer(width_m).difference(sea)
    # De band is een ring MET gat (de zee); de renderer vult alleen enkelvoudige ringen.
    # Daarom knippen we hem in tegels van 150 m zonder gaten.
    from shapely.geometry import box
    x0, y0, x1, y1 = band.bounds
    out, step = [], 150.0
    gx = x0
    while gx < x1:
        gy = y0
        while gy < y1:
            piece = band.intersection(box(gx, gy, gx + step, gy + step))
            for g in ([piece] if piece.geom_type == "Polygon" else
                      [q for q in getattr(piece, "geoms", []) if q.geom_type == "Polygon"]):
                if g.area > 20 and not list(g.interiors):
                    out.append(list(g.exterior.coords))
            gy += step
        gx += step
    return out


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
    # Wegen/paden voor gewone (niet-terrein) kaarten: gebufferde lijnen als vlakke ringen.
    # In terreinmodus zitten ze in de kleurtextuur (build_terrain).
    if os.environ.get("ROADS", "0") == "1" and os.environ.get("TERRAIN", "0") != "1":
        Wr = {"motorway": 10, "trunk": 10, "primary": 9, "secondary": 8, "tertiary": 7,
              "unclassified": 6, "residential": 6, "living_street": 5, "service": 3.5,
              "track": 3.5, "pedestrian": 4, "footway": 2, "path": 2, "cycleway": 2.5,
              "steps": 2, "bridleway": 2}
        roads, trails = [], []
        for _, row in fetch(clat, clon, radius, {"highway": list(Wr)}).iterrows():
            g = row.geometry
            hw = str(row.get("highway"))
            if g is None or g.is_empty or hw not in Wr:
                continue
            lines = ([g] if g.geom_type == "LineString"
                     else list(g.geoms) if g.geom_type == "MultiLineString" else [])
            dest = trails if hw in ("footway", "path", "cycleway", "steps", "bridleway", "track") else roads
            for ln in lines:
                for ring in buffered_rings_local(ln, clat, clon, Wr[hw] / 2):
                    dest.append({"ring": ring})
        scene["roads"], scene["trails"] = roads, trails
        print(f"  wegen: {len(roads)} + paden: {len(trails)}")
    terrain_on = os.environ.get("TERRAIN", "0") == "1"
    t_ext = terrain_extent(radius * 1.7) if terrain_on else None
    half_t = max(radius * 1.8, t_ext * 1.4) if terrain_on else None
    if os.environ.get("COASTAL", "0") == "1":
        # bij terrein moet de zee het hele hoogtenet dekken
        sea_rings = build_sea(clat, clon, max(radius * 2.5, (half_t or 0) * 1.05))
        scene["sea"] = [{"ring": r} for r in sea_rings]
        if sea_rings:
            scene["shore"] = [{"ring": r} for r in build_shore(
                sea_rings, float(os.environ.get("BEACH_W", 20)))]
    if terrain_on:
        scene["terrain"] = build_terrain(slug, clat, clon, half_t, ddir, scene, t_ext)
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
