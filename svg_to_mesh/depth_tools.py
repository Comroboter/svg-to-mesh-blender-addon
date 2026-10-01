"""Per-object depth tools: terrace by paint order and AI depth suggestions.

Works on any selected mesh objects that are flat or extruded prisms along Z
(which is what the importers produce).
"""

import bmesh
import numpy as np

from . import mesh_builder
from .core.raster import encode_png, rasterize


def linear_to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def object_color(obj):
    hexcol = obj.get("svgmesh_color")
    if isinstance(hexcol, str) and len(hexcol) == 7:
        return tuple(int(hexcol[i:i + 2], 16) / 255.0 for i in (1, 3, 5)), hexcol
    mats = obj.data.materials
    if mats and mats[0]:
        rgb = tuple(linear_to_srgb(c) for c in mats[0].diffuse_color[:3])
    else:
        rgb = (0.8, 0.8, 0.8)
    return rgb, "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)


def z_range(obj):
    zs = [v.co.z for v in obj.data.vertices]
    return (min(zs), max(zs)) if zs else (0.0, 0.0)


def paint_order(objs):
    """Objects sorted bottom layer first (importer order, else by height)."""
    def key(o):
        layer = o.get("svgmesh_layer")
        known = isinstance(layer, int)
        top = max(((o.matrix_world @ v.co).z for v in o.data.vertices), default=0.0)
        return (0 if known else 1, layer if known else 0, top, o.name)

    return sorted(objs, key=key)


def top_triangles(obj):
    """World-space XY triangles of the faces visible from above."""
    me = obj.data
    if not me.vertices:
        return np.zeros((0, 3, 2))
    me.calc_loop_triangles()
    mw = obj.matrix_world
    co = np.array([(mw @ v.co)[:] for v in me.vertices])
    flat = co[:, 2].max() - co[:, 2].min() < 1e-9
    tris = [co[list(lt.vertices), :2] for lt in me.loop_triangles if flat or lt.normal.z > 0.5]
    return np.array(tris).reshape(-1, 3, 2)


def collect_regions(objs, max_size=512):
    """Describe the objects for the AI and render a preview PNG.

    Returns (regions, png_bytes, ordered objects); region ids are 1-based.
    """
    ordered = paint_order(objs)
    layers = []
    for o in ordered:
        rgb, _hex = object_color(o)
        layers.append((rgb, top_triangles(o)))
    pts = np.concatenate([t.reshape(-1, 2) for _c, t in layers if len(t)] or [np.zeros((1, 2))])
    bounds = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
    img, to_px = rasterize(layers, bounds, max_size=max_size)
    total = 0.0
    areas = []
    for _rgb, tris in layers:
        a = 0.0
        if len(tris):
            v1 = tris[:, 1] - tris[:, 0]
            v2 = tris[:, 2] - tris[:, 0]
            a = float(np.abs(v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0]).sum() * 0.5)
        areas.append(a)
        total += a
    regions = []
    for i, (o, (rgb, tris), area) in enumerate(zip(ordered, layers, areas)):
        _rgb, hexcol = object_color(o)
        entry = {"id": i + 1, "name": o.name, "color": hexcol, "layer": i,
                 "area_percent": round(100.0 * area / total, 2) if total else 0.0}
        if len(tris):
            xy = tris.reshape(-1, 2)
            x0, y0 = to_px(xy[:, 0].min(), xy[:, 1].max())
            x1, y1 = to_px(xy[:, 0].max(), xy[:, 1].min())
            entry["bbox_px"] = [int(x0), int(y0), int(x1), int(y1)]
        regions.append(entry)
    return regions, encode_png(img), ordered


def set_depth(obj, bottom, top):
    """Rescale an object along local Z so that it spans [bottom, top] in world units.

    Flat objects are turned into closed solids first.
    """
    sz = abs(obj.matrix_world.to_scale().z) or 1.0
    oz = obj.matrix_world.translation.z
    lo, hi = (bottom - oz) / sz, (top - oz) / sz
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    zs = [v.co.z for v in bm.verts]
    if not zs:
        bm.free()
        return
    zmin, zmax = min(zs), max(zs)
    if zmax - zmin < 1e-9:
        for v in bm.verts:
            v.co.z = 0.0
        for f in bm.faces:
            if f.normal.z < 0:
                f.normal_flip()
        mesh_builder.solidify(bm, 1.0)
        zmin, zmax = 0.0, 1.0
    span = zmax - zmin
    for v in bm.verts:
        v.co.z = lo + (v.co.z - zmin) / span * (hi - lo)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()


def apply_heights(ordered, heights, base_depth):
    """heights: {object: (height, base, reason)} in multiples of base_depth."""
    for o in ordered:
        if o not in heights:
            continue
        height, base, reason = heights[o]
        set_depth(o, base * base_depth, (base + height) * base_depth)
        o["svgmesh_height"] = round(height, 3)
        o["svgmesh_base"] = round(base, 3)
        if reason:
            o["svgmesh_reason"] = reason
        elif "svgmesh_reason" in o:
            del o["svgmesh_reason"]


def terrace(ordered, base_depth, step):
    """Each layer stands on the ground and is *step* x base_depth higher than the one below."""
    heights = {o: (1.0 + step * i, 0.0, "") for i, o in enumerate(ordered)}
    apply_heights(ordered, heights, base_depth)
