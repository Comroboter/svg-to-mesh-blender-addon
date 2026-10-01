"""Per-object depth tools: terrace by paint order and AI depth suggestions.

Works on any selected mesh objects that are flat or extruded prisms along Z
(which is what the importers produce).
"""

import colorsys

import bmesh
import bpy
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


def map_colors(n):
    """*n* clearly different flat colors for the region map (no gray)."""
    out = []
    for i in range(n):
        hue = (i * 0.618033988749895) % 1.0
        light = (0.45, 0.62, 0.32)[(i // 7) % 3]
        out.append(colorsys.hls_to_rgb(hue, light, 0.85))
    return out


def collect_regions(objs, max_size=512):
    """Describe the objects for the AI and render the preview images.

    Returns (regions, [preview png, region map png], ordered objects);
    region ids are 1-based. The region map paints every region in its own
    color, so parts that share a color can be told apart.
    """
    ordered = paint_order(objs)
    layers = []
    for o in ordered:
        rgb, _hex = object_color(o)
        layers.append((rgb, top_triangles(o)))
    pts = np.concatenate([t.reshape(-1, 2) for _c, t in layers if len(t)] or [np.zeros((1, 2))])
    bounds = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
    img, to_px = rasterize(layers, bounds, max_size=max_size)
    ids = map_colors(len(layers))
    id_img, _to_px = rasterize([(c, tris) for c, (_rgb, tris) in zip(ids, layers)], bounds, max_size=max_size)
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
                 "map_color": "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in ids[i]),
                 "area_percent": round(100.0 * area / total, 2) if total else 0.0}
        if len(tris):
            xy = tris.reshape(-1, 2)
            x0, y0 = to_px(xy[:, 0].min(), xy[:, 1].max())
            x1, y1 = to_px(xy[:, 0].max(), xy[:, 1].min())
            entry["bbox_px"] = [int(x0), int(y0), int(x1), int(y1)]
        regions.append(entry)
    return regions, [encode_png(img), encode_png(id_img)], ordered


# --------------------------------------------------------------------------
# splitting objects into their separate parts
# --------------------------------------------------------------------------


def _islands(bm):
    """Lists of face indices of the edge-connected parts of a bmesh."""
    bm.faces.ensure_lookup_table()
    seen = set()
    islands = []
    for f in bm.faces:
        if f.index in seen:
            continue
        seen.add(f.index)
        stack, island = [f], []
        while stack:
            cur = stack.pop()
            island.append(cur.index)
            for e in cur.edges:
                for g in e.link_faces:
                    if g.index not in seen:
                        seen.add(g.index)
                        stack.append(g)
        islands.append(island)
    return islands


def split_parts(obj, min_share=0.002):
    """Split *obj* into one object per separate part (largest first).

    Parts smaller than *min_share* of the object's area stay together in
    one "details" object. Returns the new objects (or [obj] if there is
    nothing to split); the original object is removed.
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        islands = _islands(bm)
        if len(islands) < 2:
            return [obj]
        zs = [v.co.z for v in bm.verts]
        flat = max(zs) - min(zs) < 1e-9
        areas = [sum(bm.faces[i].calc_area() for i in isl if flat or bm.faces[i].normal.z > 0.5) for isl in islands]
        total = sum(areas) or 1.0
        order = sorted(range(len(islands)), key=lambda k: -areas[k])
        groups = [(islands[k], "part %d" % (n + 1)) for n, k in enumerate(order) if areas[k] >= min_share * total]
        small = [i for k in order if areas[k] < min_share * total for i in islands[k]]
        if small:
            groups.append((small, "details"))
        if len(groups) < 2:
            return [obj]
        parts = []
        for faces, label in groups:
            keep = set(faces)
            part = bm.copy()
            part.faces.index_update()
            bmesh.ops.delete(part, geom=[f for f in part.faces if f.index not in keep], context="FACES")
            me = obj.data.copy()
            part.to_mesh(me)
            part.free()
            new = obj.copy()
            new.data = me
            new.name = "%s %s" % (obj.name, label)
            for coll in obj.users_collection:
                coll.objects.link(new)
            parts.append(new)
    finally:
        bm.free()
    old_mesh = obj.data
    bpy.data.objects.remove(obj)
    if old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)
    return parts


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


def world_thickness(obj):
    zs = [(obj.matrix_world @ v.co).z for v in obj.data.vertices]
    return (max(zs) - min(zs)) if zs else 0.0


def world_extent(objs):
    """Largest X/Y size of the objects together (world units)."""
    xs, ys = [], []
    for o in objs:
        for v in o.data.vertices:
            co = o.matrix_world @ v.co
            xs.append(co.x)
            ys.append(co.y)
    return max(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0


def auto_base_depth(objs):
    """The thickness that 'height 1.0' should mean for these objects.

    Taken from their current thickness (divided by a stored height, so that
    applying heights repeatedly keeps the same scale); flat objects get 5 %
    of their size. This keeps the depth tools independent of object scale.
    """
    units = []
    for o in objs:
        t = world_thickness(o)
        if t <= 1e-9:
            continue
        h = o.get("svgmesh_height")
        units.append(t / h if isinstance(h, (int, float)) and h > 0 else t)
    if units:
        units.sort()
        return units[len(units) // 2]
    return 0.05 * max(world_extent(objs), 1e-6)


def resolve_base_depth(objs, base_depth):
    """(base depth to use, warning or None); 0 means automatic."""
    if base_depth <= 0:
        return auto_base_depth(objs), None
    extent = world_extent(objs)
    if extent > 0 and base_depth < 0.002 * extent:
        return base_depth, ("Base Depth %.3g m is very thin for objects %.3g m wide - set it to 0 for automatic"
                            % (base_depth, extent))
    return base_depth, None


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
