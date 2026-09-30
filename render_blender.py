"""
render_blender.py — scene_<slug>.json → 3D model-render
========================================================
Draait IN Blender:
  blender -b -P render_blender.py -- scene_delft.json out.png

Opties via env-variabelen (voor sample-generatie):
  MARK     = none | pins | hearts | markers | glow   (datum-markers)
  GREEN    = 0 | 1                                   (parken/gras tonen)
  TREES    = 0 | 1                                   (bomen tonen)
  WATERCOL = teal | blue                             (waterkleur)
  SAMPLES  = <int>   RESX = <int>   RESY = <int>     (kwaliteit/snelheid)
"""

import bpy, bmesh, json, math, sys, os
from mathutils import Vector
from mathutils.geometry import tessellate_polygon

# ── Opties ──
MARK     = os.environ.get("MARK", "none")
GREEN    = os.environ.get("GREEN", "0") == "1"
TREES    = os.environ.get("TREES", "0") == "1"
WATERCOL = os.environ.get("WATERCOL", "teal")
# Extra data-lagen. Groen/bos blijven standaard aan; wegen/spoor/bruggen/kades/
# sportvelden standaard UIT (rustige, schone look zoals oorspronkelijk).
FOREST   = os.environ.get("FOREST", "1") == "1"
PITCH    = os.environ.get("PITCH", "0") == "1"
RAIL     = os.environ.get("RAIL", "0") == "1"

# Vaste presets (de twee gekozen poster-instellingen). PRESET overschrijft
# MARK/GREEN/TREES.  1 = gloeiende hartjes, 2 = gebouw-outline + pin.
PRESET = os.environ.get("PRESET", "")
if PRESET == "1":
    MARK, GREEN, TREES = "hearts", True, True
elif PRESET == "2":
    MARK, GREEN, TREES = "outline_pins", True, True
elif PRESET == "3":
    MARK, GREEN, TREES = "hearts", True, True   # alleen hartjes, geen building-outline

TERRAIN    = os.environ.get("TERRAIN", "0") == "1"
TEXAG      = float(os.environ.get("TEXAG", 2.2))   # verticale overdrijving bergen
EXAG       = 3.2
HMAX_M     = 80
MSCALE     = 1.0          # marker-schaal; na extent-berekening aangepast
FRAME_MARGIN = 420        # marge (m) rond de datum-locaties
RES_X      = int(os.environ.get("RESX", 1500))
RES_Y      = int(os.environ.get("RESY", 2000))
SAMPLES    = int(os.environ.get("SAMPLES", 24))
WATER_Z    = 0.3
GROUND_PAD = 1.25

# Kleuren (linear-ish RGB)
C_BLD    = (0.92, 0.90, 0.87)
C_GROUND = (0.85, 0.83, 0.79)
C_WATER  = (0.10, 0.30, 0.62) if WATERCOL == "blue" else (0.08, 0.42, 0.48)
C_GREEN  = (0.42, 0.55, 0.30)
C_TREE   = (0.30, 0.46, 0.24)
C_PIN    = (0.78, 0.06, 0.10)
C_SAND   = (0.86, 0.76, 0.52)   # zandgeel
C_FOREST = (0.24, 0.40, 0.20)   # donkergroen bos
C_PITCH  = (0.34, 0.66, 0.38)   # sportveld-groen (iets feller)
C_RAIL   = (0.58, 0.57, 0.55)   # spoor: subtiel grijs (tussen wit en zwart)
C_LANDMK = (0.85, 0.62, 0.20)   # landmark-uitlichting (warm goud)
C_BRIDGE = (0.80, 0.78, 0.74)   # brug-dek (licht)


def add_cap(bm, ring, z, want_up):
    """Sluit een footprint op hoogte z met eigen punten; trianguleert en zet
    normalen expliciet. Zie docs/TODO.md voor nette ear-clipping."""
    vs = [bm.verts.new((x, y, z)) for x, y in ring]
    faces = []
    for tri in tessellate_polygon([[v.co for v in vs]]):
        try:
            faces.append(bm.faces.new([vs[i] for i in tri]))
        except ValueError:
            pass
    if not faces:
        c = bm.verts.new((sum(p[0] for p in ring) / len(ring),
                          sum(p[1] for p in ring) / len(ring), z))
        n = len(ring)
        for i in range(n):
            try:
                faces.append(bm.faces.new([vs[i], vs[(i + 1) % n], c]))
            except ValueError:
                pass
    for fc in faces:
        fc.normal_update()
        if (fc.normal.z >= 0) != want_up:
            fc.normal_flip()


def extrude_ring(bm, ring, h, emissive_mat_index=None, z0=0.0):
    """Bouw een gesloten geextrudeerd volume van een footprint (bodem z0, top h)."""
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return
    bcx = sum(p[0] for p in ring) / len(ring)
    bcy = sum(p[1] for p in ring) / len(ring)
    add_cap(bm, ring, h, want_up=True)
    add_cap(bm, ring, z0, want_up=False)
    top = [bm.verts.new((x, y, h)) for x, y in ring]
    bot = [bm.verts.new((x, y, z0)) for x, y in ring]
    n = len(ring)
    for i in range(n):
        j = (i + 1) % n
        try:
            fq = bm.faces.new([bot[i], bot[j], top[j], top[i]])
        except ValueError:
            continue
        fq.normal_update()
        fcx = (bot[i].co.x + bot[j].co.x) / 2
        fcy = (bot[i].co.y + bot[j].co.y) / 2
        if fq.normal.x * (fcx - bcx) + fq.normal.y * (fcy - bcy) < 0:
            fq.normal_flip()


def point_in_ring(px, py, ring):
    inside = False
    n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


# ── Args na '--' ──
argv = sys.argv
argv = argv[argv.index("--") + 1:] if "--" in argv else []
scene_file = argv[0] if argv else "scene_delft.json"
out_file   = argv[1] if len(argv) > 1 else "render_delft.png"
here = os.path.dirname(os.path.abspath(__file__))
scene_path = scene_file if os.path.isabs(scene_file) else os.path.join(here, scene_file)
out_path   = out_file   if os.path.isabs(out_file)   else os.path.join(here, out_file)

with open(scene_path) as f:
    data = json.load(f)

buildings = data["buildings"]
water     = data.get("water", [])
green     = data.get("green", [])
trees     = data.get("trees", [])
sand      = data.get("sand", [])
forest    = data.get("forest", [])
pitch     = data.get("pitch", [])
rail      = data.get("rail", [])
bridge    = data.get("bridge", [])
pier      = data.get("pier", [])
dates     = data.get("dates", [])
print(f"[render] {len(buildings)} geb, {len(water)} water, {len(green)} groen, "
      f"{len(trees)} bomen, {len(dates)} datums | MARK={MARK} GREEN={GREEN} "
      f"TREES={TREES} WATER={WATERCOL}")

# ── Lege scene ──
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ── Hoogtenet (alleen TERRAIN=1 met terrain-data in de scene) ──
terrain = data.get("terrain") if TERRAIN else None
if terrain:
    _tn, _th, _tstep = terrain["n"], terrain["half"], terrain["step"]
    _tz = [v * TEXAG for v in terrain["z"]]


def tz(x, y):
    """Terreinhoogte (m, al overdreven) op lokale (x, y); 0 zonder terrein."""
    if not terrain:
        return 0.0
    fx = min(max((x + _th) / _tstep, 0.0), _tn - 1.001)
    fy = min(max((y + _th) / _tstep, 0.0), _tn - 1.001)
    i, j = int(fx), int(fy)
    ax, ay = fx - i, fy - j
    z = _tz
    return (z[j * _tn + i] * (1 - ax) * (1 - ay) + z[j * _tn + i + 1] * ax * (1 - ay) +
            z[(j + 1) * _tn + i] * (1 - ax) * ay + z[(j + 1) * _tn + i + 1] * ax * ay)


def make_material(name, rgb, rough=0.7, emit=0.0, emit_rgb=None):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    if emit > 0:
        bsdf.inputs["Emission Color"].default_value = (*(emit_rgb or rgb), 1.0)
        bsdf.inputs["Emission Strength"].default_value = emit
    return mat


mat_bld   = make_material("Buildings", C_BLD, 0.75, emit=0.12, emit_rgb=C_BLD)
# Kustlocatie: alleen de ZEE (zeewaarts vlak) wordt blauw; de grond blijft grijs
# zodat wegen/gaten op het land niet blauw worden.
COASTAL_SLUGS = {"denhaag"}
_coastal = data.get("slug") in COASTAL_SLUGS or os.environ.get("COASTAL", "0") == "1"
mat_ground = make_material("Ground", C_GROUND, 0.85)
mat_sea   = make_material("Sea", (0.10, 0.34, 0.55), 0.4)
mat_water = make_material("Water", C_WATER, 0.25)
# Emissie-bodem op groen (zelfde wegwerk-fix als gebouwen): voorkomt zwarte
# vlakken als een groen-normaal omlaag wijst. Zie docs/TODO.md.
mat_green = make_material("Green", C_GREEN, 0.85, emit=0.16, emit_rgb=C_GREEN)
mat_sand  = make_material("Sand", C_SAND, 0.9, emit=0.12, emit_rgb=C_SAND)
mat_tree  = make_material("Tree", C_TREE, 0.8)
mat_forest = make_material("Forest", C_FOREST, 0.85, emit=0.12, emit_rgb=C_FOREST)
mat_pitch  = make_material("Pitch", C_PITCH, 0.8, emit=0.14, emit_rgb=C_PITCH)
mat_rail   = make_material("Rail", C_RAIL, 0.7, emit=0.10, emit_rgb=C_RAIL)
mat_landmk = make_material("Landmark", C_LANDMK, 0.5, emit=0.30, emit_rgb=C_LANDMK)
mat_bridge = make_material("Bridge", C_BRIDGE, 0.7, emit=0.12, emit_rgb=C_BRIDGE)
mat_road  = make_material("Road", (0.70, 0.69, 0.66), 0.8, emit=0.10, emit_rgb=(0.70, 0.69, 0.66))
mat_trail = make_material("Trail", (0.62, 0.55, 0.45), 0.9, emit=0.10, emit_rgb=(0.62, 0.55, 0.45))
mat_pin   = make_material("Pin", C_PIN, 0.35, emit=0.5, emit_rgb=C_PIN)
mat_glow  = make_material("Glow", C_PIN, 0.4, emit=3.0, emit_rgb=(1.0, 0.15, 0.18))
mat_edge  = make_material("Edge", (0.80, 0.05, 0.08), 0.4, emit=0.8,
                          emit_rgb=(1.0, 0.10, 0.12))
mat_heart = make_material("Heart", C_PIN, 0.3, emit=2.2, emit_rgb=(1.0, 0.12, 0.16))
# Ryanair-kleuren voor het vliegtuig
mat_pl_white  = make_material("PlWhite", (0.93, 0.93, 0.94), 0.4)
mat_pl_blue   = make_material("PlBlue", (0.04, 0.10, 0.45), 0.3)
mat_pl_yellow = make_material("PlYellow", (0.98, 0.80, 0.05), 0.3,
                              emit=0.3, emit_rgb=(0.98, 0.80, 0.05))
mat_route = make_material("Route", (1.0, 0.45, 0.55), 0.3,
                          emit=2.8, emit_rgb=(1.0, 0.35, 0.5))

# ── Gebouwen als één mesh (landmarks apart, in goud + iets hoger) ──
LANDMARKS = os.environ.get("LANDMARKS", "0") == "1"   # standaard uit
bm = bmesh.new()
lbm = bmesh.new()
minx = miny =  1e9
maxx = maxy = -1e9
for b in buildings:
    ring = b["ring"]
    for x, y in ring:
        minx = min(minx, x); maxx = max(maxx, x)
        miny = min(miny, y); maxy = max(maxy, y)
    h = min(b["h"], HMAX_M) * EXAG
    zb, zc = 0.0, 0.0
    if terrain:      # bodem tot onder de laagste hoek (ingegraven), dak boven het gemiddelde
        zs = [tz(x, y) for x, y in ring]
        zb, zc = min(zs) - 4.0, sum(zs) / len(zs)
    if LANDMARKS and b.get("lm"):
        extrude_ring(lbm, ring, zc + h * 1.15, z0=zb)   # landmark: iets hoger
    else:
        extrude_ring(bm, ring, zc + h, z0=zb)

mesh = bpy.data.meshes.new("BuildingsMesh")
bm.to_mesh(mesh); bm.free()
obj = bpy.data.objects.new("Buildings", mesh)
obj.data.materials.append(mat_bld)
scene.collection.objects.link(obj)
for p in mesh.polygons:
    p.use_smooth = False
mesh.update()

if len(lbm.faces) > 0:
    lmesh = bpy.data.meshes.new("Landmarks")
    lbm.to_mesh(lmesh)
    lo = bpy.data.objects.new("Landmarks", lmesh)
    lo.data.materials.append(mat_landmk)
    scene.collection.objects.link(lo)
    for p in lmesh.polygons:
        p.use_smooth = False
lbm.free()

# Frame op de cluster van datum-locaties. Ver uitschieters tellen niet mee voor
# de zoom → die belanden net op het randje, terwijl de rest goed in beeld staat.
# Aspect-correct (portret is smaller in X) zodat niets links/rechts wegvalt.
radius_m = data.get("radius_m", 800)
# FIT_ALL=1: geen outlier-rejectie → álle plekken in beeld (breed stadsoverzicht).
FIT_ALL = os.environ.get("FIT_ALL", "0") == "1"
if dates:
    import statistics as _st
    mx = _st.median([d["x"] for d in dates])
    my = _st.median([d["y"] for d in dates])
    dist = [math.hypot(d["x"] - mx, d["y"] - my) for d in dates]
    md = _st.median(dist) if dist else 0
    thr = max(2.2 * md + 250, 350)
    cluster = dates if FIT_ALL else ([d for d, di in zip(dates, dist) if di <= thr] or dates)
    # Framing-ankers (bv. Utrecht Centraal): wél in beeld, geen marker.
    frame_pts = cluster + data.get("anchors", [])
    cx = sum(d["x"] for d in frame_pts) / len(frame_pts)
    cy = sum(d["y"] for d in frame_pts) / len(frame_pts)
    aspect = RES_X / RES_Y
    half = max((max(abs(d["y"] - cy), abs(d["x"] - cx) / aspect)
                for d in frame_pts), default=0)
    extent = max(850, 2 * half + 2 * FRAME_MARGIN)
    if not FIT_ALL:                       # bij FIT_ALL geen straal-cap (alles moet passen)
        extent = min(extent, radius_m * 1.7)
    # Vliegtuig-locatie: strak inzoomen op het toestel zodat het groot en
    # herkenbaar in beeld komt (anders verdwijnt het op de lege tarmac).
    _plane = next((d for d in dates if d.get("icon") == "airplane"), None)
    if _plane is not None:
        cx, cy = _plane["x"], _plane["y"]
        extent = float(os.environ.get("PLANE_EXTENT", 1900))
else:
    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2
    extent = max(maxx - minx, maxy - miny)
# Markers groter op grotere kaarten (zichtbaar bij spread steden)
if terrain:
    # ruimer uitzoomen zodat de bergen in beeld komen: 1.6x de stadsuitsnede (1800-4500 m)
    extent = max(extent, float(os.environ.get("TERRAIN_EXTENT") or min(4500.0, max(1800.0, 1.6 * extent))))
MSCALE = min(3.2, max(1.0, extent / 1500))
print(f"[render] extent {extent:.0f}m, centrum ({cx:.0f},{cy:.0f}), MSCALE {MSCALE:.2f}")


def build_flat(rings, z, mat, name):
    if not rings:
        return
    fbm = bmesh.new()
    for r in rings:
        rr = r[:-1] if r and r[0] == r[-1] else r
        if len(rr) >= 3:
            add_cap(fbm, rr, z, want_up=True)
    m = bpy.data.meshes.new(name)
    fbm.to_mesh(m); fbm.free()
    o = bpy.data.objects.new(name, m)
    o.data.materials.append(mat)
    scene.collection.objects.link(o)


# ── Grond ── (ruim groter dan het frame zodat er nooit grijze achtergrond
# doorschijnt bij de gekantelde camera)
bpy.ops.mesh.primitive_plane_add(size=extent * 6, location=(cx, cy, -0.2))
bpy.context.active_object.data.materials.append(mat_ground)

# ── Bergterrein: hoogtenet met de landbedekking als kleurtextuur ──
if terrain:
    tbm_ = bmesh.new()
    _vs = [tbm_.verts.new((-_th + i * _tstep, -_th + j * _tstep, _tz[j * _tn + i]))
           for j in range(_tn) for i in range(_tn)]
    for j in range(_tn - 1):
        for i in range(_tn - 1):
            tbm_.faces.new([_vs[j * _tn + i], _vs[j * _tn + i + 1],
                            _vs[(j + 1) * _tn + i + 1], _vs[(j + 1) * _tn + i]])
    uv = tbm_.loops.layers.uv.new("UVMap")
    for f in tbm_.faces:
        for l in f.loops:
            l[uv].uv = ((l.vert.co.x + _th) / (2 * _th), (l.vert.co.y + _th) / (2 * _th))
    tmesh_ = bpy.data.meshes.new("Terrain")
    tbm_.to_mesh(tmesh_); tbm_.free()
    for p_ in tmesh_.polygons:
        p_.use_smooth = True
    mat_terrain = bpy.data.materials.new("Terrain")
    mat_terrain.use_nodes = True
    _bsdf = mat_terrain.node_tree.nodes.get("Principled BSDF")
    _img = bpy.data.images.load(os.path.join(os.path.dirname(scene_path), terrain["cover"]))
    _tex = mat_terrain.node_tree.nodes.new("ShaderNodeTexImage")
    _tex.image = _img
    _tex.interpolation = "Linear"
    mat_terrain.node_tree.links.new(_tex.outputs["Color"], _bsdf.inputs["Base Color"])
    mat_terrain.node_tree.links.new(_tex.outputs["Color"], _bsdf.inputs["Emission Color"])
    _bsdf.inputs["Roughness"].default_value = 0.9
    _bsdf.inputs["Emission Strength"].default_value = 0.10
    tmesh_.materials.append(mat_terrain)
    tobj_ = bpy.data.objects.new("Terrain", tmesh_)
    scene.collection.objects.link(tobj_)
    print(f"[render] terrein {_tn}x{_tn}, reliëf {max(_tz):.0f} m (TEXAG {TEXAG})")

# ── Zee (alleen kustlocaties): groot blauw half-vlak aan de zeekant van het strand.
# Kustlijn-oriëntatie komt uit het zandstrook zélf (PCA op de zandpunten), niet uit
# de centroid-vector gebouw→zand: die heuristiek week ~45° af bij een diagonale kust
# (Scheveningen ligt NO-ZW) → de westkant links van het zand bleef grijs i.p.v. blauw.
if _coastal and data.get("sea") and not terrain:
    # Automatische zee uit de OSM-kustlijn (export_osm.build_sea)
    build_flat([z["ring"] for z in data["sea"]], 0.05, mat_sea, "Sea")
    if data.get("shore"):    # automatische zandstrook langs de kustlijn
        build_flat([z["ring"] for z in data["shore"]], 0.13, mat_sand, "Shore")
elif _coastal and sand and buildings and not terrain:
    spts = [p for s in sand for p in s["ring"]]
    n = len(spts)
    mx = sum(p[0] for p in spts) / n
    my = sum(p[1] for p in spts) / n
    sxx = sum((p[0] - mx) ** 2 for p in spts) / n
    syy = sum((p[1] - my) ** 2 for p in spts) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in spts) / n
    tr = sxx + syy
    l1 = tr / 2 + math.sqrt(max(0.0, tr * tr / 4 - (sxx * syy - sxy * sxy)))
    tx, ty = sxy, l1 - sxx            # principale as = langs de kust
    tl = math.hypot(tx, ty) or 1.0
    tx, ty = tx / tl, ty / tl
    nx, ny = -ty, tx                  # loodrecht = land↔zee-richting
    bxc = sum(sum(p[0] for p in b["ring"]) / len(b["ring"])
              for b in buildings) / len(buildings)
    byc = sum(sum(p[1] for p in b["ring"]) / len(b["ring"])
              for b in buildings) / len(buildings)
    if nx * (bxc - mx) + ny * (byc - my) > 0:   # normaal moet WEG van gebouwen wijzen
        nx, ny = -nx, -ny
    # Naad ZEEWAARTS van alle gebouwen (niet op het zand-zwaartepunt). Bij een diep
    # strand (bv. Knokke) ligt dat zwaartepunt ver in zee -> een naad daar snijdt dwars
    # door de stad en zet gebouwen onder blauw. De zeewaartse gebouwrand (max projectie
    # op de zee-normaal, +30m) garandeert dat geen enkel gebouw onder blauw komt; het
    # zand wordt hoger getekend en dekt het strand tot aan het water.
    edge = max(nx * (sum(p[0] for p in b["ring"]) / len(b["ring"]) - mx) +
               ny * (sum(p[1] for p in b["ring"]) / len(b["ring"]) - my)
               for b in buildings) + 30.0
    ox, oy = mx + nx * edge, my + ny * edge
    BIG = extent * 6
    quad = [(ox + tx * BIG, oy + ty * BIG),
            (ox - tx * BIG, oy - ty * BIG),
            (ox - tx * BIG + nx * BIG, oy - ty * BIG + ny * BIG),
            (ox + tx * BIG + nx * BIG, oy + ty * BIG + ny * BIG)]
    build_flat([quad], 0.05, mat_sea, "Sea")

# ── Zand (strand/duin) — boven de zee ──
if sand and not terrain:
    build_flat([s["ring"] for s in sand], 0.12, mat_sand, "Sand")

# ── Wegen en paden (ROADS=1, niet-terrein; bij terrein zitten ze in de textuur) ──
if data.get("roads") and not terrain:
    build_flat([r["ring"] for r in data["roads"]], 0.10, mat_road, "Roads")
if data.get("trails") and not terrain:
    build_flat([r["ring"] for r in data["trails"]], 0.11, mat_trail, "Trails")

# ── Groen ──
if GREEN and not terrain:
    build_flat([g["ring"] for g in green], 0.08, mat_green, "Green")
    if FOREST:
        build_flat([f["ring"] for f in forest], 0.085, mat_forest, "Forest")
    if PITCH:
        build_flat([p["ring"] for p in pitch], 0.10, mat_pitch, "Pitch")

# ── Spoor ──
if RAIL:
    build_flat([r["ring"] for r in rail], 0.07, mat_rail, "Rail")

# ── Kades / pieren (licht, net boven water) ──
if os.environ.get("PIER", "0") == "1":
    build_flat([p["ring"] for p in pier], WATER_Z + 0.4, mat_bridge, "Pier")

# ── Water ──
if not terrain:
    build_flat([w["ring"] for w in water], WATER_Z, mat_water, "Water")

# ── Bruggen (boven het water) — alleen schone, grote bruggen (Maas-oversteken).
# De OSM-bufferlaag bevat corrupte ringen met verdwaalde coords (→ reuzepolygoon
# die de render breekt); filter die eruit en houd alleen forse bruggen over.
if os.environ.get("BRIDGE", "0") == "1" and bridge:
    rmax = radius_m * 1.5
    good = []
    for b in bridge:
        r = b["ring"]
        xs = [p[0] for p in r]; ys = [p[1] for p in r]
        if max(max(abs(v) for v in xs), max(abs(v) for v in ys)) > rmax:
            continue                                  # verdwaalde coords → skip
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        if 100 <= diag <= 2500:                       # alleen grote bruggen
            good.append(r)
    if good:
        print(f"[render] {len(good)} grote bruggen (van {len(bridge)} ruw)")
        build_flat(good, WATER_Z + 1.5, mat_bridge, "Bridge")

# ── Bomen (lage cones in één mesh) ──
_tt = (terrain.get("trees") or []) if terrain else []
if TREES and (trees or _tt):
    trees = list(trees) + _tt
    _ts = max(1.0, MSCALE) if terrain else 1.0   # bomen schalen mee met de uitsnede
    tbm = bmesh.new()
    for tx, ty in trees:
        zt = tz(tx, ty)
        apex = tbm.verts.new((tx, ty, zt + 11.0 * _ts))
        ring = [tbm.verts.new((tx + 3.2 * _ts * math.cos(2*math.pi*k/6),
                               ty + 3.2 * _ts * math.sin(2*math.pi*k/6), zt + 0.3))
                for k in range(6)]
        for k in range(6):
            try:
                tbm.faces.new([apex, ring[k], ring[(k + 1) % 6]])
            except ValueError:
                pass
    bmesh.ops.recalc_face_normals(tbm, faces=tbm.faces)
    tm = bpy.data.meshes.new("Trees")
    tbm.to_mesh(tm); tbm.free()
    to = bpy.data.objects.new("Trees", tm)
    to.data.materials.append(mat_tree)
    scene.collection.objects.link(to)
    for p in tm.polygons:
        p.use_smooth = False


def building_height_at(px, py):
    for b in buildings:
        if point_in_ring(px, py, b["ring"]):
            r_ = b["ring"]
            zc_ = (sum(tz(x, y) for x, y in r_) / len(r_)) if terrain else 0.0
            return zc_ + min(b["h"], HMAX_M) * EXAG, b["ring"]
    return tz(px, py), None


# ── Datum-markers ──
def add_pin(x, y, z0):
    S = MSCALE
    bpy.ops.mesh.primitive_cone_add(vertices=12, radius1=2.6*S, radius2=0.0,
                                    depth=26*S, location=(x, y, z0 + 13*S))
    cone = bpy.context.active_object
    cone.rotation_euler = (math.radians(180), 0, 0)  # punt naar beneden
    cone.data.materials.append(mat_pin)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=9*S, location=(x, y, z0 + 30*S))
    head = bpy.context.active_object
    head.data.materials.append(mat_pin)
    for o in (cone, head):
        for p in o.data.polygons:
            p.use_smooth = (o is head)


def add_heart(x, y, z0, scale=1.0):
    pts = []
    for k in range(40):
        t = 2 * math.pi * k / 40
        u = 16 * math.sin(t) ** 3
        v = 13*math.cos(t) - 5*math.cos(2*t) - 2*math.cos(3*t) - math.cos(4*t)
        pts.append((u, v))
    s = 0.9 * MSCALE * scale
    th = 1.5 * MSCALE * scale
    zc = z0 + 16 * MSCALE * scale
    hbm = bmesh.new()
    front = [hbm.verts.new((x + u*s, y - th, zc + v*s)) for u, v in pts]
    back  = [hbm.verts.new((x + u*s, y + th, zc + v*s)) for u, v in pts]
    for tri in tessellate_polygon([[Vector((vv.co.x, vv.co.z, 0)) for vv in front]]):
        for ring in (front, back):
            try: hbm.faces.new([ring[i] for i in tri])
            except ValueError: pass
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        try: hbm.faces.new([front[i], front[j], back[j], back[i]])
        except ValueError: pass
    bmesh.ops.recalc_face_normals(hbm, faces=hbm.faces)
    hm = bpy.data.meshes.new("Heart"); hbm.to_mesh(hm); hbm.free()
    ho = bpy.data.objects.new("Heart", hm); ho.data.materials.append(mat_heart)
    scene.collection.objects.link(ho)


def add_glow_marker(x, y, z0):
    bpy.ops.mesh.primitive_cylinder_add(radius=11, depth=1.0,
                                        location=(x, y, z0 + 1.0))
    bpy.context.active_object.data.materials.append(mat_glow)
    bpy.ops.mesh.primitive_cylinder_add(radius=2.2, depth=55,
                                        location=(x, y, z0 + 28))
    bpy.context.active_object.data.materials.append(mat_glow)


def _box(bm, x0, y0, z0, x1, y1, z1, mi=0):
    """Voeg een doos (8 hoeken, 6 vlakken) toe aan een bmesh, met materiaal-index."""
    v = [bm.verts.new(p) for p in [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]]
    for f in [(0,1,2,3), (4,5,6,7), (0,1,5,4), (1,2,6,5),
              (2,3,7,6), (3,0,4,7)]:
        try:
            fc = bm.faces.new([v[i] for i in f]); fc.material_index = mi
        except ValueError:
            pass


def _import_model(path, x, y, target_size=100, rot_z=40, rot_x=0):
    """Importeer een .glb/.gltf/.obj/.stl, voeg samen, schaal naar target_size (m)
    en zet het op de grond bij (x, y). Geeft (succes, top_z) terug."""
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    try:
        if ext in (".glb", ".gltf"):
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".obj":
            bpy.ops.wm.obj_import(filepath=path)
        elif ext == ".stl":
            bpy.ops.wm.stl_import(filepath=path)
        else:
            return False, 0
    except Exception as e:
        print(f"[render] model-import mislukt: {e}")
        return False, 0
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    if not meshes:
        return False, 0
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    # Alleen bij een parent (gltf-root) de transforms wegbakken; bij een kale OBJ
    # NIET — anders telt onze rotatie dubbel (toestel komt rechtop te staan).
    if obj.parent:
        bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    bpy.context.view_layer.update()
    dims = obj.dimensions
    size = max(dims.x, dims.y, dims.z, 1e-3)
    s = (target_size * MSCALE) / size
    obj.scale = (s, s, s)
    obj.rotation_euler = (math.radians(rot_x), 0, math.radians(rot_z))
    obj.location = (x, y, 0.0)
    bpy.context.view_layer.update()
    # Centreer het model-zwaartepunt op (x, y) en zet de onderkant op de grond
    # (handmatig via wereld-bbox; origin_set is headless onbetrouwbaar).
    ws = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    obj.location.x += x - sum(p.x for p in ws) / 8
    obj.location.y += y - sum(p.y for p in ws) / 8
    obj.location.z -= min(p.z for p in ws)
    bpy.context.view_layer.update()
    top_z = max((obj.matrix_world @ Vector(c)).z for c in obj.bound_box)
    # De OBJ-import wijst het mtl-materiaal soms NIET toe (obj.data.materials leeg).
    # Maak daarom zelf een materiaal met de livery-textuur en wijs het toe. De UV-
    # map ('UVMap') komt uit de OBJ, dus de textuur mapt correct.
    diffuse = os.path.join(os.path.dirname(path), "texture_diffuse.png")
    mat = bpy.data.materials.new("Livery")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Metallic"].default_value = 0.0
    if os.path.exists(diffuse):
        img = bpy.data.images.load(diffuse, check_existing=True)
        tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
        tex.image = img
        mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 0.30
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    print(f"[model] {os.path.basename(path)} geplaatst, top_z {top_z:.1f}")
    return True, top_z


def add_airplane(x, y, z0):
    """Ryanair-achtige jet, geparkeerd op de grond. Geeft (mx, my, top_z) terug:
    het ankerpunt waar de loop de marker ruim bóven het toestel zet.
    Gebruikt een echt model als AIRPLANE_MODEL is gezet, anders een low-poly doos.
    Materiaal-index: 0=wit, 1=blauw, 2=geel."""
    model = os.environ.get("AIRPLANE_MODEL", "")
    if not model:
        for d in (os.path.join(here, "models"), here):
            for cand in ("airplane.obj", "airplane.glb", "airplane.gltf"):
                p = os.path.join(d, cand)
                if os.path.exists(p):
                    model = p; break
            if model:
                break
    if model and os.path.exists(model):
        # OBJ-romp ligt langs Z (rechtop) → +90° om X om plat te leggen; rz=20
        # (90° met de klok mee t.o.v. eerder) → neus richting het gebouw. Iets
        # noordwaarts verschoven zodat de neus niet tegen het gebouw zit.
        oy = y + 140 * MSCALE     # noordwaarts → ruimte tussen neus en gebouw
        ok, top_z = _import_model(model, x, oy,
                                  target_size=240, rot_x=90, rot_z=20)
        if ok:
            return x, oy, top_z                  # marker plaatst de loop hierboven
    S = 2.0 * MSCALE
    abm = bmesh.new()
    _box(abm, -20, -2.2, 2.0, 22, 2.2, 6.5, 0)    # romp (wit)
    _box(abm, 22, -1.2, 3.0, 26, 1.2, 5.5, 0)     # neus (wit)
    _box(abm, -6, -19, 3.2, 6, 19, 4.0, 0)        # vleugels (wit)
    _box(abm, -2, -19.5, 4.0, 2, -16.5, 8.5, 1)   # winglet links (blauw)
    _box(abm, -2, 16.5, 4.0, 2, 19.5, 8.5, 1)     # winglet rechts (blauw)
    _box(abm, -22, -0.6, 6.0, -16, 0.6, 15.0, 1)  # staartvin (blauw)
    _box(abm, -21, -8, 5.5, -15, 8, 6.2, 0)       # staartvleugel (wit)
    _box(abm, -4, -11, 1.0, 5, -7, 3.4, 1)        # motor links (blauw)
    _box(abm, -4, 7, 1.0, 5, 11, 3.4, 1)          # motor rechts (blauw)
    _box(abm, -18, -2.25, 1.9, 14, 2.25, 2.7, 2)  # gele striping
    bmesh.ops.recalc_face_normals(abm, faces=abm.faces)
    am = bpy.data.meshes.new("Airplane"); abm.to_mesh(am); abm.free()
    for m in (mat_pl_white, mat_pl_blue, mat_pl_yellow):
        am.materials.append(m)
    ao = bpy.data.objects.new("Airplane", am)
    ao.location = (x, y, 0.0)
    ao.scale = (S, S, S)
    ao.rotation_euler = (0, 0, math.radians(40))
    scene.collection.objects.link(ao)
    for p in am.polygons:
        p.use_smooth = False
    return x, y, 6.5 * S


def add_route(points, z):
    """Gloeiende lijn die de plekken in volgorde verbindt (zwevend op hoogte z)."""
    r = 2.2 * MSCALE
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        a = Vector((x0, y0, z)); b = Vector((x1, y1, z))
        vec = b - a
        if vec.length < 1e-3:
            continue
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=vec.length,
                                            location=tuple((a + b) / 2))
        cyl = bpy.context.active_object
        cyl.rotation_euler = Vector((0, 0, 1)).rotation_difference(
            vec.normalized()).to_euler()
        cyl.data.materials.append(mat_route)
    for x, y in points:                       # bolletjes op de knooppunten
        bpy.ops.mesh.primitive_uv_sphere_add(radius=r * 1.5, location=(x, y, z))
        bpy.context.active_object.data.materials.append(mat_route)


def add_outline(x, y, z0, ring):
    rr = ring[:-1] if ring[0] == ring[-1] else ring
    obm = bmesh.new()
    top = [obm.verts.new((px, py, z0)) for px, py in rr]
    bot = [obm.verts.new((px, py, 0.0)) for px, py in rr]
    n = len(rr)
    for i in range(n):
        j = (i + 1) % n
        try:
            obm.faces.new([bot[i], bot[j], top[j], top[i]])
        except ValueError:
            pass
    om = bpy.data.meshes.new("Outline")
    obm.to_mesh(om); obm.free()
    oo = bpy.data.objects.new("Outline", om)
    oo.data.materials.append(mat_edge)
    scene.collection.objects.link(oo)
    wf = oo.modifiers.new("wire", "WIREFRAME")
    wf.thickness = 2.2
    wf.use_replace = True


# Combineerbare markerstijlen
do_pins    = MARK in ("pins", "outline_pins")
do_hearts  = MARK in ("hearts", "outline_hearts")
do_markers = MARK == "markers"
do_outline = MARK in ("outline", "outline_pins", "outline_hearts")
do_glow    = MARK == "glow"

# Route-lijn (los aan/uit te zetten, combineert met elke marker)
if os.environ.get("ROUTE", "0") == "1" and len(dates) >= 2:
    add_route([(d["x"], d["y"]) for d in dates], z=70 * MSCALE)

if MARK != "none" and dates:
    glow_bm = bmesh.new() if do_glow else None
    for d in dates:
        x, y = d["x"], d["y"]
        z0, ring = building_height_at(x, y)
        icon = d.get("icon", "")
        if icon == "airplane":          # locatie-specifiek model + marker erboven
            ax, ay, atop = add_airplane(x, y, z0)
            base = atop + 8 * MSCALE     # net boven de hoogste punt van het toestel
            if do_pins:
                add_pin(ax, ay, base)
            else:                        # heart (preset 3 default) — groot genoeg
                add_heart(ax, ay, base, scale=4.0)
            continue
        if do_outline and ring is not None:
            add_outline(x, y, z0, ring)
        if do_pins:
            add_pin(x, y, z0)
        if do_hearts:
            add_heart(x, y, z0)
        if do_markers:
            add_glow_marker(x, y, z0)
        if do_glow and ring is not None:
            rr = ring[:-1] if ring[0] == ring[-1] else ring
            bcx = sum(p[0] for p in rr) / len(rr)
            bcy = sum(p[1] for p in rr) / len(rr)
            scaled = [(bcx + (px-bcx)*1.08, bcy + (py-bcy)*1.08) for px, py in rr]
            extrude_ring(glow_bm, scaled, z0 * 0.99)
    if do_glow and glow_bm is not None:
        gm = bpy.data.meshes.new("Glow"); glow_bm.to_mesh(gm); glow_bm.free()
        go = bpy.data.objects.new("Glow", gm); go.data.materials.append(mat_glow)
        scene.collection.objects.link(go)

# ── Licht ── (GOLDEN=1 → warm gouden-uur: lagere, warmere zon + langere schaduwen)
GOLDEN = os.environ.get("GOLDEN", "0") == "1"
sun_data = bpy.data.lights.new("Sun", type="SUN")
sun = bpy.data.objects.new("Sun", sun_data)
if GOLDEN:
    sun_data.energy = 2.6
    sun_data.angle = math.radians(2.5)
    sun_data.color = (1.0, 0.78, 0.50)                 # warme avondzon
    sun.rotation_euler = (math.radians(74), math.radians(10), math.radians(-60))
else:
    sun_data.energy = 2.0
    sun_data.angle = math.radians(2.5)
    sun.rotation_euler = (math.radians(50), math.radians(15), math.radians(-55))
if terrain:   # lage zon zodat hellingen schaduw krijgen
    sun_data.energy = 3.2
    sun.rotation_euler = (math.radians(float(os.environ.get("SUN_EL", 62))), 0, math.radians(-65))
scene.collection.objects.link(sun)

world = bpy.data.worlds.new("World")
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if GOLDEN:
    bg.inputs["Color"].default_value = (0.96, 0.82, 0.66, 1.0)   # warme schemerlucht
    bg.inputs["Strength"].default_value = 0.35
else:
    bg.inputs["Color"].default_value = (0.88, 0.90, 0.94, 1.0)
    bg.inputs["Strength"].default_value = 0.25
scene.world = world

# ── Camera: gekantelde ortho top-down ──
cam_data = bpy.data.cameras.new("Cam")
cam_data.type = "ORTHO"
cam_data.ortho_scale = extent * 1.04   # strak frame: gebouwen vullen de poster
cam_data.clip_start = 1.0
cam_data.clip_end = extent * 8
cam = bpy.data.objects.new("Cam", cam_data)
_zc = tz(cx, cy)
_cam_y, _cam_z = (extent * float(os.environ.get("CAM_Y", 1.1)), extent * float(os.environ.get("CAM_Z", 0.9))) if terrain else (extent * 0.55, extent * 1.0)
eye = Vector((cx, cy - _cam_y, _zc + _cam_z))
direction = (Vector((cx, cy, _zc)) - eye).normalized()
cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
cam.location = eye
scene.collection.objects.link(cam)
scene.camera = cam

# ── Render-instellingen ──
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y

# Screen-posities van datum-locaties wegschrijven (voor 2D emoji/label-laag)
if dates:
    from bpy_extras.object_utils import world_to_camera_view
    dg = bpy.context.evaluated_depsgraph_get()
    pos = []
    for d in dates:
        z0, _ = building_height_at(d["x"], d["y"])
        co = world_to_camera_view(scene, cam, Vector((d["x"], d["y"], z0)))
        pos.append({"name": d["name"], "px": co.x * RES_X,
                    "py": (1 - co.y) * RES_Y})
    with open(out_path + ".pos.json", "w", encoding="utf-8") as pf:
        json.dump(pos, pf)
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = out_path
try:
    scene.view_settings.view_transform = "Standard"
except Exception:
    pass
scene.view_settings.exposure = -0.2

scene.render.engine = "CYCLES"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dtype in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = dtype
            prefs.get_devices()
            if any(d.type == dtype for d in prefs.devices):
                for d in prefs.devices:
                    d.use = (d.type == dtype)
                scene.cycles.device = "GPU"
                print(f"[render] GPU: {dtype}")
                break
        except Exception:
            continue
except Exception:
    pass

print("[render] renderen...")
bpy.ops.render.render(write_still=True)
print(f"[render] klaar: {out_path}")
