"""
export_glb.py — scene_<slug>.json → geoptimaliseerde .glb voor de web-viewer
============================================================================
Draait IN Blender (headless):
  blender -b -P export_glb.py -- data/scene_delft.json web/models/delft.glb

Bouwt hetzelfde witte stadsmodel als render_blender.py (zelfde geometrie + BAG-
hoogtes), maar exporteert het als glTF-Binary i.p.v. te renderen:
  - web-PBR-materialen (baseColor + roughness, metallic 0, GEEN emissie; de
    viewer doet de belichting),
  - gecentreerd op de origin (X/Y), grond op z=0 → exporter zet +Y up,
  - geen camera's/lampen; DRACO-meshcompressie aan,
  - giant achtergrond-plane/zee vervangen door een nette ground op de crop.

Env (zelfde als de render-invocatie per stad):
  GREEN/TREES/FOREST = 0|1 (default 1)   FIT_ALL = 0|1   BRIDGE = 0|1
  AIRPLANE_MODEL = pad (anders models/airplane.obj)   PLANE_EXTENT
"""

import bpy, bmesh, json, math, sys, os
from mathutils import Vector
from mathutils.geometry import tessellate_polygon

GREEN  = os.environ.get("GREEN", "1") == "1"
TREES  = os.environ.get("TREES", "1") == "1"
FOREST = os.environ.get("FOREST", "1") == "1"
FIT_ALL = os.environ.get("FIT_ALL", "0") == "1"
BRIDGE = os.environ.get("BRIDGE", "0") == "1"
TERRAIN = os.environ.get("TERRAIN", "0") == "1"
TEXAG   = float(os.environ.get("TEXAG", 2.2))         # bergen: zelfde overdrijving als de render
GLB_TEX = int(os.environ.get("GLB_TEX", 1600))        # textuurgrootte terrein in de .glb (px)

EXAG       = 3.2          # zelfde hoogte-overdrijving als de render (look matcht)
HMAX_M     = 80
FRAME_MARGIN = 420
RES_X, RES_Y = 2480, 3508   # A2-aspect (alleen voor MSCALE, niet voor een camera)
WATER_Z    = 0.3

# Kleuren — zelfde palet als render_blender.py (de goedgekeurde look)
C_BLD    = (0.92, 0.90, 0.87)
C_GROUND = (0.85, 0.83, 0.79)
C_WATER  = (0.08, 0.42, 0.48)
C_GREEN  = (0.42, 0.55, 0.30)
C_TREE   = (0.30, 0.46, 0.24)
C_SAND   = (0.86, 0.76, 0.52)
C_SEA    = (0.10, 0.34, 0.55)
C_FOREST = (0.24, 0.40, 0.20)
C_BRIDGE = (0.80, 0.78, 0.74)
C_HEART  = (0.89, 0.11, 0.24)   # roze/rood accent voor de date-plekken


# ── geometrie-helpers (zelfde als render_blender.py) ──
def add_cap(bm, ring, z, want_up):
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


def extrude_ring(bm, ring, h, z0=0.0):
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


# ── args ──
argv = sys.argv
argv = argv[argv.index("--") + 1:] if "--" in argv else []
scene_file = argv[0] if argv else "data/scene_delft.json"
out_file   = argv[1] if len(argv) > 1 else "web/models/delft.glb"
here = os.path.dirname(os.path.abspath(__file__))
scene_path = scene_file if os.path.isabs(scene_file) else os.path.join(here, scene_file)
out_path   = out_file   if os.path.isabs(out_file)   else os.path.join(here, out_file)
os.makedirs(os.path.dirname(out_path), exist_ok=True)

with open(scene_path) as f:
    data = json.load(f)

buildings = data["buildings"]
water  = data.get("water", []); green = data.get("green", [])
trees  = data.get("trees", []); sand = data.get("sand", [])
forest = data.get("forest", []); bridge = data.get("bridge", [])
dates  = data.get("dates", [])
slug   = data.get("slug", "")
print(f"[glb] {slug}: {len(buildings)} geb, {len(water)} water, {len(green)} groen, "
      f"{len(trees)} bomen, {len(dates)} datums")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ── hoogtenet (TERRAIN=1 + terrain-data in de scene) ──
terrain = data.get("terrain") if TERRAIN else None
if terrain:
    _tn, _th, _tstep = terrain["n"], terrain["half"], terrain["step"]
    _tz = [v * TEXAG for v in terrain["z"]]


def tz(x, y):
    """Terreinhoogte (m, overdreven) op lokale (x, y); 0 zonder terrein."""
    if not terrain:
        return 0.0
    fx = min(max((x + _th) / _tstep, 0.0), _tn - 1.001)
    fy = min(max((y + _th) / _tstep, 0.0), _tn - 1.001)
    i, j = int(fx), int(fy)
    ax, ay = fx - i, fy - j
    z = _tz
    return (z[j * _tn + i] * (1 - ax) * (1 - ay) + z[j * _tn + i + 1] * ax * (1 - ay) +
            z[(j + 1) * _tn + i] * (1 - ax) * ay + z[(j + 1) * _tn + i + 1] * ax * ay)


def make_material(name, rgb, rough=0.8):
    """Web-PBR: baseColor + roughness, metallic 0, geen emissie."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = 0.0
    return mat


mat_bld   = make_material("Buildings", C_BLD, 0.8)
mat_ground = make_material("Ground", C_GROUND, 0.9)
mat_water = make_material("Water", C_WATER, 0.35)
mat_green = make_material("Green", C_GREEN, 0.9)
mat_sand  = make_material("Sand", C_SAND, 0.9)
mat_sea   = make_material("Sea", C_SEA, 0.4)
mat_tree  = make_material("Tree", C_TREE, 0.85)
mat_forest = make_material("Forest", C_FOREST, 0.9)
mat_bridge = make_material("Bridge", C_BRIDGE, 0.75)
mat_heart = make_material("DateSpot", C_HEART, 0.4)

COASTAL_SLUGS = {"denhaag"}
_coastal = slug in COASTAL_SLUGS or os.environ.get("COASTAL", "0") == "1"

# ── gebouwen als één mesh ──
bm = bmesh.new()
minx = miny = 1e9; maxx = maxy = -1e9
for b in buildings:
    ring = b["ring"]
    for x, y in ring:
        minx = min(minx, x); maxx = max(maxx, x)
        miny = min(miny, y); maxy = max(maxy, y)
    hb = min(b["h"], HMAX_M) * EXAG
    zb, zc = 0.0, 0.0
    if terrain:      # ingegraven tot onder de laagste hoek, dak boven het gemiddelde
        zs = [tz(x, y) for x, y in ring]
        zb, zc = min(zs) - 4.0, sum(zs) / len(zs)
    extrude_ring(bm, ring, zc + hb, z0=zb)
mesh = bpy.data.meshes.new("BuildingsMesh")
bm.to_mesh(mesh); bm.free()
obj = bpy.data.objects.new("Buildings", mesh)
obj.data.materials.append(mat_bld)
scene.collection.objects.link(obj)
for p in mesh.polygons:
    p.use_smooth = False
mesh.update()

# ── framing-centrum + MSCALE (zelfde logica als de render, voor marker-grootte) ──
radius_m = data.get("radius_m", 800)
import statistics as _st
if dates:
    mx = _st.median([d["x"] for d in dates]); my = _st.median([d["y"] for d in dates])
    dist = [math.hypot(d["x"] - mx, d["y"] - my) for d in dates]
    md = _st.median(dist) if dist else 0
    thr = max(2.2 * md + 250, 350)
    cluster = dates if FIT_ALL else ([d for d, di in zip(dates, dist) if di <= thr] or dates)
    frame_pts = cluster + data.get("anchors", [])
    cx = sum(d["x"] for d in frame_pts) / len(frame_pts)
    cy = sum(d["y"] for d in frame_pts) / len(frame_pts)
    aspect = RES_X / RES_Y
    half = max((max(abs(d["y"] - cy), abs(d["x"] - cx) / aspect) for d in frame_pts), default=0)
    extent = max(850, 2 * half + 2 * FRAME_MARGIN)
    if not FIT_ALL:
        extent = min(extent, radius_m * 1.7)
    _plane = next((d for d in dates if d.get("icon") == "airplane"), None)
    if _plane is not None:
        cx, cy = _plane["x"], _plane["y"]
        extent = float(os.environ.get("PLANE_EXTENT", 1900))
else:
    cx = (minx + maxx) / 2; cy = (miny + maxy) / 2
    extent = max(maxx - minx, maxy - miny)
MSCALE = min(3.2, max(1.0, extent / 1500))
print(f"[glb] extent {extent:.0f}m, centrum ({cx:.0f},{cy:.0f}), MSCALE {MSCALE:.2f}")


# Crop-rechthoek = gebouw-bbox + marge. De platte lagen (water/groen/zee) bevatten
# soms polygonen die ver buiten de stad doorlopen (bv. de Schie-gracht, 3+ km) →
# in de render crop de camera die weg; voor de .glb clippen we ze expliciet zodat
# de bounding box de stad volgt en niets ver van de origin staat.
_cm = max(150.0, 0.12 * max(maxx - minx, maxy - miny))
CROP = (minx - _cm, miny - _cm, maxx + _cm, maxy + _cm)
if terrain:   # bergen: uitsnede ruim rond het frame-centrum (binnen het hoogtenet)
    _hc = min(float(os.environ.get("TERRAIN_EXTENT", 4500)) * 0.75, _th - 2 * _tstep)
    CROP = (min(CROP[0], max(cx - _hc, -_th + _tstep)), min(CROP[1], max(cy - _hc, -_th + _tstep)),
            max(CROP[2], min(cx + _hc, _th - _tstep)), max(CROP[3], min(cy + _hc, _th - _tstep)))


def clip_ring(ring, box):
    """Sutherland-Hodgman: clip een polygoon op een asgerichte rechthoek."""
    x0, y0, x1, y1 = box
    poly = ring[:-1] if ring and ring[0] == ring[-1] else list(ring)
    def _clip(poly, inside, isect):
        out = []; n = len(poly)
        for i in range(n):
            a = poly[i]; b = poly[(i + 1) % n]
            ia, ib = inside(a), inside(b)
            if ia:
                out.append(a)
                if not ib: out.append(isect(a, b))
            elif ib:
                out.append(isect(a, b))
        return out
    def ix(a, b, xc):
        t = (xc - a[0]) / ((b[0] - a[0]) or 1e-12); return (xc, a[1] + t * (b[1] - a[1]))
    def iy(a, b, yc):
        t = (yc - a[1]) / ((b[1] - a[1]) or 1e-12); return (a[0] + t * (b[0] - a[0]), yc)
    poly = _clip(poly, lambda p: p[0] >= x0, lambda a, b: ix(a, b, x0))
    poly = _clip(poly, lambda p: p[0] <= x1, lambda a, b: ix(a, b, x1))
    poly = _clip(poly, lambda p: p[1] >= y0, lambda a, b: iy(a, b, y0))
    poly = _clip(poly, lambda p: p[1] <= y1, lambda a, b: iy(a, b, y1))
    return poly


def build_flat(rings, z, mat, name, clip=True):
    if not rings:
        return
    fbm = bmesh.new()
    for r in rings:
        rr = r[:-1] if r and r[0] == r[-1] else r
        if clip:
            rr = clip_ring(rr, CROP)
        if len(rr) >= 3:
            add_cap(fbm, rr, z, want_up=True)
    if not fbm.faces:
        fbm.free(); return
    m = bpy.data.meshes.new(name)
    fbm.to_mesh(m); fbm.free()
    o = bpy.data.objects.new(name, m)
    o.data.materials.append(mat)
    scene.collection.objects.link(o)


# ── ground: nette rechthoek op exact de crop (geen giant plane) ──
gx0, gy0, gx1, gy1 = CROP
ground = [(gx0, gy0), (gx1, gy0), (gx1, gy1), (gx0, gy1)]
if terrain:
    # hoogtenet op de crop, kleurtextuur (verkleind) als baseColor
    i0, i1 = int((gx0 + _th) / _tstep), int((gx1 + _th) / _tstep) + 1
    j0, j1 = int((gy0 + _th) / _tstep), int((gy1 + _th) / _tstep) + 1
    tbm_ = bmesh.new()
    _vs = {}
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            _vs[(i, j)] = tbm_.verts.new((-_th + i * _tstep, -_th + j * _tstep, _tz[j * _tn + i]))
    for j in range(j0, j1):
        for i in range(i0, i1):
            tbm_.faces.new([_vs[(i, j)], _vs[(i + 1, j)], _vs[(i + 1, j + 1)], _vs[(i, j + 1)]])
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
    _bsdf.inputs["Roughness"].default_value = 0.9
    _bsdf.inputs["Metallic"].default_value = 0.0
    _img = bpy.data.images.load(os.path.join(os.path.dirname(scene_path), terrain["cover"]))
    if max(_img.size) > GLB_TEX:
        _img.scale(GLB_TEX, GLB_TEX)
    _tex = mat_terrain.node_tree.nodes.new("ShaderNodeTexImage")
    _tex.image = _img
    mat_terrain.node_tree.links.new(_tex.outputs["Color"], _bsdf.inputs["Base Color"])
    tmesh_.materials.append(mat_terrain)
    scene.collection.objects.link(bpy.data.objects.new("Terrain", tmesh_))
    print(f"[glb] terrein {i1 - i0}x{j1 - j0} cellen, reliëf {max(_tz):.0f} m")
else:
    build_flat([ground], -0.2, mat_ground, "Ground", clip=False)

# ── zee (kustlocaties): half-vlak op de crop, oriëntatie via PCA op het zand ──
if _coastal and data.get("sea") and not terrain:
    build_flat([z["ring"] for z in data["sea"]], 0.05, mat_sea, "Sea")
    if data.get("shore"):
        build_flat([z["ring"] for z in data["shore"]], 0.13, mat_sand, "Shore")
elif _coastal and sand and buildings and not terrain:
    spts = [p for s in sand for p in s["ring"]]
    n = len(spts)
    smx = sum(p[0] for p in spts) / n; smy = sum(p[1] for p in spts) / n
    sxx = sum((p[0]-smx)**2 for p in spts)/n; syy = sum((p[1]-smy)**2 for p in spts)/n
    sxy = sum((p[0]-smx)*(p[1]-smy) for p in spts)/n
    tr = sxx + syy
    l1 = tr/2 + math.sqrt(max(0.0, tr*tr/4 - (sxx*syy - sxy*sxy)))
    tx, ty = sxy, l1 - sxx
    tl = math.hypot(tx, ty) or 1.0; tx, ty = tx/tl, ty/tl
    nx, ny = -ty, tx
    bxc = sum(sum(p[0] for p in b["ring"])/len(b["ring"]) for b in buildings)/len(buildings)
    byc = sum(sum(p[1] for p in b["ring"])/len(b["ring"]) for b in buildings)/len(buildings)
    if nx*(bxc-smx) + ny*(byc-smy) > 0:
        nx, ny = -nx, -ny
    # Naad ZEEWAARTS van alle gebouwen (niet op het zand-zwaartepunt): bij een diep
    # strand (Knokke) ligt dat zwaartepunt ver in zee -> naad snijdt door de stad. De
    # zeewaartse gebouwrand (+30m) voorkomt dat er gebouwen onder blauw komen.
    edge = max(nx * (sum(p[0] for p in b["ring"]) / len(b["ring"]) - smx) +
               ny * (sum(p[1] for p in b["ring"]) / len(b["ring"]) - smy)
               for b in buildings) + 30.0
    ox, oy = smx + nx * edge, smy + ny * edge
    BIG = max(maxx - minx, maxy - miny) * 3.0
    quad = [(ox + tx*BIG, oy + ty*BIG), (ox - tx*BIG, oy - ty*BIG),
            (ox - tx*BIG + nx*BIG, oy - ty*BIG + ny*BIG),
            (ox + tx*BIG + nx*BIG, oy + ty*BIG + ny*BIG)]
    build_flat([quad], 0.05, mat_sea, "Sea")

if not terrain:     # bij terrein zit dit al in de kleurtextuur
    if sand:
        build_flat([s["ring"] for s in sand], 0.12, mat_sand, "Sand")
    if GREEN:
        build_flat([g["ring"] for g in green], 0.08, mat_green, "Green")
        if FOREST:
            build_flat([f["ring"] for f in forest], 0.085, mat_forest, "Forest")
    build_flat([w["ring"] for w in water], WATER_Z, mat_water, "Water")

# ── bruggen (Rotterdam) ──
if BRIDGE and bridge:
    rmax = radius_m * 1.5; good = []
    for b in bridge:
        r = b["ring"]; xs = [p[0] for p in r]; ys = [p[1] for p in r]
        if max(max(abs(v) for v in xs), max(abs(v) for v in ys)) > rmax:
            continue
        diag = math.hypot(max(xs)-min(xs), max(ys)-min(ys))
        if 100 <= diag <= 2500:
            good.append(r)
    if good:
        build_flat(good, WATER_Z + 1.5, mat_bridge, "Bridge")

# ── bomen (low-poly cones in één mesh) ──
_tt = (terrain.get("trees") or []) if terrain else []
if TREES and (trees or _tt):
    trees = list(trees) + _tt
    _ts = max(1.0, MSCALE) if terrain else 1.0   # bomen schalen mee met de uitsnede
    tbm = bmesh.new()
    for tx, ty in trees:
        zt = tz(tx, ty)
        apex = tbm.verts.new((tx, ty, zt + 11.0 * _ts))
        ring = [tbm.verts.new((tx + 3.2*_ts*math.cos(2*math.pi*k/6),
                               ty + 3.2*_ts*math.sin(2*math.pi*k/6), zt + 0.3)) for k in range(6)]
        for k in range(6):
            try: tbm.faces.new([apex, ring[k], ring[(k+1) % 6]])
            except ValueError: pass
    bmesh.ops.recalc_face_normals(tbm, faces=tbm.faces)
    tm = bpy.data.meshes.new("Trees"); tbm.to_mesh(tm); tbm.free()
    to = bpy.data.objects.new("Trees", tm); to.data.materials.append(mat_tree)
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


def add_heart(x, y, z0, scale=1.0):
    pts = []
    for k in range(40):
        t = 2*math.pi*k/40
        u = 16*math.sin(t)**3
        v = 13*math.cos(t) - 5*math.cos(2*t) - 2*math.cos(3*t) - math.cos(4*t)
        pts.append((u, v))
    s = 0.9 * MSCALE * scale; th = 1.5 * MSCALE * scale; zc = z0 + 16*MSCALE*scale
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


def _import_model(path, x, y, target_size=100, rot_z=40, rot_x=0):
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    try:
        if ext in (".glb", ".gltf"): bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".obj": bpy.ops.wm.obj_import(filepath=path)
        elif ext == ".stl": bpy.ops.wm.stl_import(filepath=path)
        else: return False, 0
    except Exception as e:
        print(f"[glb] model-import mislukt: {e}"); return False, 0
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    if not meshes: return False, 0
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes: o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1: bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    if obj.parent:
        bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    bpy.context.view_layer.update()
    dims = obj.dimensions
    s = (target_size * MSCALE) / max(dims.x, dims.y, dims.z, 1e-3)
    obj.scale = (s, s, s)
    obj.rotation_euler = (math.radians(rot_x), 0, math.radians(rot_z))
    obj.location = (x, y, 0.0)
    bpy.context.view_layer.update()
    ws = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    obj.location.x += x - sum(p.x for p in ws) / 8
    obj.location.y += y - sum(p.y for p in ws) / 8
    obj.location.z -= min(p.z for p in ws)
    bpy.context.view_layer.update()
    top_z = max((obj.matrix_world @ Vector(c)).z for c in obj.bound_box)
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
    obj.data.materials.clear(); obj.data.materials.append(mat)
    return True, top_z


def add_airplane(x, y, z0):
    model = os.environ.get("AIRPLANE_MODEL", "")
    if not model:
        for d in (os.path.join(here, "models"), here):
            for cand in ("airplane.obj", "airplane.glb", "airplane.gltf"):
                p = os.path.join(d, cand)
                if os.path.exists(p): model = p; break
            if model: break
    if model and os.path.exists(model):
        oy = y + 140 * MSCALE
        ok, top_z = _import_model(model, x, oy, target_size=240, rot_x=90, rot_z=20)
        if ok:
            return x, oy, top_z
    return x, y, 12.0


# ── date-plekken: hartjes (zelfde accent als de render) + Kraków-toestel ──
if dates:
    for d in dates:
        x, y = d["x"], d["y"]
        z0, _ = building_height_at(x, y)
        if d.get("icon") == "airplane":
            ax, ay, atop = add_airplane(x, y, z0)
            add_heart(ax, ay, atop + 8*MSCALE, scale=4.0)
        else:
            add_heart(x, y, z0)

# ── vertices lassen (verliesvrij): extrude_ring maakt cap- en muurpunten los →
# merge-by-distance halveert de vertex-count en krimpt de draco-glb fors. Het
# vliegtuig (Livery-materiaal, eigen UV's) slaan we over. ──
for o in list(scene.collection.objects):
    if o.type != "MESH":
        continue
    if any(m and m.name.startswith("Livery") for m in o.data.materials):
        continue
    wbm = bmesh.new(); wbm.from_mesh(o.data)
    bmesh.ops.remove_doubles(wbm, verts=wbm.verts, dist=0.01)
    wbm.to_mesh(o.data); wbm.free(); o.data.update()

# ── centreren op de origin: gebruik het echte geometrie-zwaartepunt (XY) zodat
# het model altijd netjes rond de origin staat (handover-eis: geen offset). ──
glo = [1e9, 1e9]; ghi = [-1e9, -1e9]; gzmin = 1e9
for o in scene.collection.objects:
    if o.type != "MESH":
        continue
    for c in o.bound_box:
        w = o.matrix_world @ Vector(c)
        glo[0] = min(glo[0], w.x); ghi[0] = max(ghi[0], w.x)
        glo[1] = min(glo[1], w.y); ghi[1] = max(ghi[1], w.y)
        gzmin = min(gzmin, w.z)
gctrx = (glo[0] + ghi[0]) / 2; gctry = (glo[1] + ghi[1]) / 2
for o in scene.collection.objects:
    o.location.x -= gctrx
    o.location.y -= gctry
    if terrain:                     # laagste punt van het terrein op z=0 (handover-eis: grond op 0)
        o.location.z -= gzmin

# ── exporteren: GLB + DRACO, +Y up, geen camera's/lampen ──
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(
    filepath=out_path, export_format="GLB", use_selection=True,
    export_yup=True, export_apply=True, export_cameras=False, export_lights=False,
    export_image_format="JPEG", export_jpeg_quality=82,
    export_draco_mesh_compression_enable=True, export_draco_mesh_compression_level=6,
    export_draco_position_quantization=12, export_draco_normal_quantization=8,
)
sz = os.path.getsize(out_path) / 1e6
tri = sum(len(o.data.polygons) for o in scene.collection.objects if o.type == "MESH")
print(f"[glb] KLAAR: {out_path}  ({sz:.2f} MB, ~{tri} faces)")
