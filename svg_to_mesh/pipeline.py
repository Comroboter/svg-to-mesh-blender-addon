"""VectorShapes -> Blender objects (shared by every operator)."""

from dataclasses import dataclass, field

import bpy

from . import mesh_builder
from .core.geometry import shape_to_polys, shapes_bounds


@dataclass
class ImportSettings:
    mesh: mesh_builder.MeshSettings = field(default_factory=mesh_builder.MeshSettings)
    curve_tolerance: float = 0.0005  # relative to the largest dimension
    grid_size: float = 0.02  # relative to the largest output dimension
    depth: float = 0.0  # absolute, output units
    scale_mode: str = "FIT"  # FIT | REAL | KEEP
    target_size: float = 1.0  # FIT: largest dimension in output units
    unit_scale: float = 1.0  # REAL: output units per source unit
    origin: str = "CENTER"  # CENTER | BOTTOM_LEFT | KEEP
    separate: str = "ONE"  # ONE | COLOR | SHAPE
    ignore_white: bool = False
    include_strokes: bool = True
    layer_offset: float = 0.0
    create_materials: bool = True


def is_white(color):
    return color is not None and min(color) > 0.94


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def color_hex(color):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c * 255)))) for c in color)


def get_material(color):
    name = "SVG " + color_hex(color)
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    lin = tuple(srgb_to_linear(c) for c in color)
    mat.diffuse_color = (*lin, 1.0)
    if bpy.app.version < (5, 0, 0) and not mat.use_nodes:
        mat.use_nodes = True  # always on (and deprecated) since Blender 5.0
    tree = getattr(mat, "node_tree", None)
    if tree is not None:
        for node in tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                node.inputs["Base Color"].default_value = (*lin, 1.0)
                break
    return mat


def prepare_polys(shapes, settings):
    """Flatten shapes and map them into output space.

    Returns (polys, bounds_out, size_out).
    """
    b = shapes_bounds(shapes)
    if b is None:
        return [], None, 0.0
    max_dim = max(b[2] - b[0], b[3] - b[1]) or 1.0
    tol = max_dim * max(settings.curve_tolerance, 1e-6)

    polys = []
    for i, shape in enumerate(shapes):
        for p in shape_to_polys(shape, tol, include_stroke=settings.include_strokes):
            p.source_index = i
            polys.append(p)
    if not polys:
        return [], None, 0.0

    xs = [x for p in polys for c in p.contours for x, _y in c]
    ys = [y for p in polys for c in p.contours for _x, y in c]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    dim = max(maxx - minx, maxy - miny) or 1.0

    if settings.scale_mode == "FIT":
        s = settings.target_size / dim
    elif settings.scale_mode == "REAL":
        s = settings.unit_scale
    else:
        s = 1.0
    if settings.origin == "CENTER":
        ox, oy = (minx + maxx) * 0.5, (miny + maxy) * 0.5
    elif settings.origin == "BOTTOM_LEFT":
        ox, oy = minx, miny
    else:
        ox = oy = 0.0
    for p in polys:
        p.contours = [[((x - ox) * s, (y - oy) * s) for x, y in c] for c in p.contours]
    bounds = ((minx - ox) * s, (miny - oy) * s, (maxx - ox) * s, (maxy - oy) * s)
    return polys, bounds, dim * s


def build_objects(context, shapes, settings, name, collection=None, matrix=None):
    """Create mesh objects from VectorShapes. Returns the new objects."""
    polys, bounds, size = prepare_polys(shapes, settings)
    if not polys:
        return []

    group_keys = []
    group_of = []
    group_info = []
    for p in polys:
        if settings.separate == "COLOR":
            key = color_hex(p.color) if p.color is not None else "none"
        elif settings.separate == "SHAPE":
            key = p.source_index
        else:
            key = "all"
        if key not in group_keys:
            group_keys.append(key)
            group_info.append(p)
        group_of.append(group_keys.index(key))
    knockout = [settings.ignore_white and is_white(p.color) for p in polys]

    ms = mesh_builder.MeshSettings(**vars(settings.mesh))
    ms.grid_size = max(settings.grid_size, 1e-4) * size
    ms.depth = settings.depth
    ms.merge_distance = size * 1e-6
    tri = mesh_builder.triangulate(polys, group_of, knockout, ms)

    if collection is None:
        collection = context.collection or context.scene.collection
    objects = []
    group_faces = mesh_builder.faces_by_group(tri)
    for gid, first in enumerate(group_info):
        bm = mesh_builder.build_bmesh(tri, gid, ms, group_faces.get(gid, []))
        if not bm.faces:
            bm.free()
            continue
        mesh_builder.add_planar_uvs(bm, bounds)
        if settings.layer_offset:
            z = settings.layer_offset * len(objects)
            for v in bm.verts:
                v.co.z += z
        if len(group_info) == 1:
            obj_name = name
        elif settings.separate == "COLOR":
            obj_name = "%s %s" % (name, group_keys[gid])
        else:
            obj_name = "%s %s" % (name, first.name)
        me = bpy.data.meshes.new(obj_name)
        bm.to_mesh(me)
        bm.free()
        if settings.create_materials and first.color is not None:
            if settings.separate == "ONE":
                colors = {}
                for p in polys:
                    if p.color is not None and not is_white(p.color):
                        colors.setdefault(color_hex(p.color), p.color)
                col = next(iter(colors.values()), first.color)
            else:
                col = first.color
            me.materials.append(get_material(col))
        obj = bpy.data.objects.new(obj_name, me)
        # remembered for the "Depth per Object" tools (paint order, color)
        obj["svgmesh_layer"] = gid
        if first.color is not None:
            obj["svgmesh_color"] = color_hex(first.color)
        if matrix is not None:
            obj.matrix_world = matrix
        collection.objects.link(obj)
        objects.append(obj)
    return objects


def select_objects(context, objects):
    if not objects:
        return
    for o in context.view_layer.objects:
        if o is not None and o.select_get():
            o.select_set(False)
    for o in objects:
        o.select_set(True)
    context.view_layer.objects.active = objects[0]
