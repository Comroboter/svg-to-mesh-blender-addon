"""Finishing tools: a base plate under the artwork (optionally with a hole
for a key ring) and merging the parts into one closed solid."""

import math

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty
from bpy.types import Operator

from . import depth_tools, mesh_builder, pipeline
from .core.geometry import PolyShape, signed_area, stroke_polygons

PLATE_COLOR = (0.85, 0.85, 0.85)


def _meshes(context):
    return [o for o in context.selected_objects if o.type == "MESH" and o.data.vertices]


# --------------------------------------------------------------------------
# outlines
# --------------------------------------------------------------------------


def outline_loops(objs):
    """World-space XY boundary loops of the faces seen from above.

    Outer loops run counter-clockwise, holes clockwise.
    """
    loops = []
    for obj in objs:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        try:
            bm.transform(obj.matrix_world)
            bm.normal_update()
            zs = [v.co.z for v in bm.verts]
            flat = not zs or max(zs) - min(zs) < 1e-9
            top = {f for f in bm.faces if flat or f.normal.z > 0.5}
            if flat:  # flat meshes may face down: walk them as seen from above
                flip = {f for f in top if f.normal.z < 0}
            else:
                flip = set()
            nxt = {}
            for f in top:
                for loop in f.loops:
                    if sum(1 for g in loop.edge.link_faces if g in top) != 1:
                        continue
                    a, b = loop.vert, loop.link_loop_next.vert
                    if f in flip:
                        a, b = b, a
                    nxt.setdefault(a, []).append(b)
            while nxt:
                start = next(iter(nxt))
                loop, cur = [], start
                while cur in nxt:
                    loop.append((cur.co.x, cur.co.y))
                    targets = nxt[cur]
                    nb = targets.pop()
                    if not targets:
                        del nxt[cur]
                    cur = nb
                    if cur is start:
                        break
                if len(loop) >= 3 and abs(signed_area(loop)) > 0:
                    loops.append(loop)
        finally:
            bm.free()
    return loops


def _circle(cx, cy, r, tol):
    n = max(16, int(math.ceil(math.pi / math.acos(max(-1.0, 1.0 - tol / r)))) if r > tol else 16)
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def plate_shapes(loops, margin, size, hole=0.0, wall=0.0):
    """PolyShapes of a plate covering *loops* (holes filled) grown by *margin*,
    plus optionally a tab with a hole of diameter *hole* at the top.

    Returns (shapes, knockout flags).
    """
    tol = max(size * 0.0008, 1e-9)
    shapes = [PolyShape([loop], "nonzero") for loop in loops]
    if margin > 0:
        for loop in loops:
            if signed_area(loop) > 0:  # outer outlines only; holes are filled anyway
                ring = stroke_polygons(loop, True, 2.0 * margin, "round", "round", tol=tol)
                if ring:
                    shapes.append(PolyShape(ring, "nonzero"))
    knockout = [False] * len(shapes)
    if hole > 0 and loops:
        r = hole * 0.5
        wall = wall if wall > 0 else max(r * 0.8, margin)
        pts = [p for loop in loops for p in loop]
        top_y = max(y for _x, y in pts)
        cx_all = (min(x for x, _y in pts) + max(x for x, _y in pts)) * 0.5
        # the highest artwork point nearest to the middle
        near = [p for p in pts if p[1] >= top_y - size * 0.02]
        tx = min(near, key=lambda p: abs(p[0] - cx_all))[0]
        cy = top_y + wall * 0.5 + r  # the hole stays clear of the artwork
        shapes.append(PolyShape([_circle(tx, cy, r + wall, tol)], "nonzero"))
        knockout.append(False)
        shapes.append(PolyShape([_circle(tx, cy, r, tol)], "nonzero"))
        knockout.append(True)
    return shapes, knockout


def build_plate(objs, margin, thickness, hole=0.0, topology="NGON"):
    """Return a new bmesh for a plate under *objs* (world space), or None."""
    loops = outline_loops(objs)
    if not loops:
        return None
    xs = [x for loop in loops for x, _y in loop]
    ys = [y for loop in loops for _x, y in loop]
    size = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    shapes, knockout = plate_shapes(loops, margin, size, hole)
    ms = mesh_builder.MeshSettings(topology=topology, overlap="VISIBLE")
    ms.merge_distance = size * 1e-6
    ms.grid_size = size * 0.02
    ms.depth = thickness
    tri = mesh_builder.triangulate(shapes, [0] * len(shapes), knockout, ms)
    bm = mesh_builder.build_bmesh(tri, 0, ms)
    if not bm.faces:
        bm.free()
        return None
    bottom = min((o.matrix_world @ v.co).z for o in objs for v in o.data.vertices)
    bmesh.ops.translate(bm, verts=bm.verts[:], vec=(0.0, 0.0, bottom - thickness))
    return bm


class SVGMESH_OT_base_plate(Operator):
    """Add a base plate under the selected objects, following their outline with a margin.
Optionally with a hole for a key ring"""

    bl_idname = "object.svgmesh_base_plate"
    bl_label = "Add Base Plate"
    bl_options = {"REGISTER", "UNDO"}

    margin: FloatProperty(
        name="Margin", subtype="PERCENTAGE", default=4.0, min=0.0, soft_max=20.0,
        description="How far the plate reaches beyond the artwork, relative to its size",
    )
    thickness: FloatProperty(
        name="Thickness", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0,
        description="Plate thickness. 0 = automatic (the typical thickness of the selected objects)",
    )
    hole: EnumProperty(
        name="Hole",
        items=[("NONE", "No Hole", ""), ("TOP", "Key Ring Hole", "A tab with a hole at the top")],
        default="NONE",
    )
    hole_size: FloatProperty(
        name="Hole Size", subtype="PERCENTAGE", default=8.0, min=0.5, soft_max=30.0,
        description="Hole diameter, relative to the artwork size",
    )
    topology: EnumProperty(name="Topology", items=[
        ("NGON", "Clean N-Gons", ""), ("TRIS", "Triangles", ""), ("QUADS", "Quads", "")], default="NGON")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(_meshes(context))

    def execute(self, context):
        objs = [o for o in _meshes(context) if not o.get("svgmesh_plate")]
        if not objs:
            self.report({"WARNING"}, "Select the artwork, not only a plate")
            return {"CANCELLED"}
        pts = [(o.matrix_world @ v.co) for o in objs for v in o.data.vertices]
        size = max(max(p.x for p in pts) - min(p.x for p in pts), max(p.y for p in pts) - min(p.y for p in pts))
        thickness = self.thickness or depth_tools.auto_base_depth(objs)
        hole = size * self.hole_size / 100.0 if self.hole == "TOP" else 0.0
        bm = build_plate(objs, size * self.margin / 100.0, thickness, hole, self.topology)
        if bm is None:
            self.report({"WARNING"}, "No outline found")
            return {"CANCELLED"}
        name = objs[0].name.split(" #")[0] + " Plate"
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        me.materials.append(pipeline.get_material(PLATE_COLOR))
        plate = bpy.data.objects.new(name, me)
        plate["svgmesh_plate"] = True
        for coll in objs[0].users_collection:
            coll.objects.link(plate)
        for o in context.selected_objects:
            o.select_set(False)
        plate.select_set(True)
        context.view_layer.objects.active = plate
        return {"FINISHED"}


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------


def merge_solids(context, objs):
    """Boolean-union *objs* into one new mesh object. Returns (object, open edges)."""
    target = objs[0]
    others = objs[1:]
    coll = bpy.data.collections.new("svgmesh merge")
    context.scene.collection.children.link(coll)
    mod = target.modifiers.new("svgmesh merge", "BOOLEAN")
    transfer = hasattr(mod, "material_mode")
    try:
        for o in others:
            coll.objects.link(o)
        mod.operation = "UNION"
        mod.operand_type = "COLLECTION"
        mod.collection = coll
        solvers = bpy.types.BooleanModifier.bl_rna.properties["solver"].enum_items.keys()
        mod.solver = "MANIFOLD" if "MANIFOLD" in solvers else "EXACT"  # Manifold: Blender 4.5+
        if transfer:
            mod.material_mode = "TRANSFER"
        depsgraph = context.evaluated_depsgraph_get()
        me = bpy.data.meshes.new_from_object(target.evaluated_get(depsgraph))
    finally:
        target.modifiers.remove(mod)
        bpy.data.collections.remove(coll)
    if not transfer:
        # older Blender: keep at least the materials of all parts available
        for o in objs:
            for m in o.data.materials:
                if m is not None and m.name not in me.materials:
                    me.materials.append(m)
    name = target.name.split(" #")[0] + " merged"
    me.name = name
    obj = bpy.data.objects.new(name, me)
    obj.matrix_world = target.matrix_world
    for c in target.users_collection:
        c.objects.link(obj)
    bm = bmesh.new()
    bm.from_mesh(me)
    open_edges = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()
    return obj, open_edges


class SVGMESH_OT_merge_solid(Operator):
    """Merge the selected objects (colors, parts, base plate) into one closed solid for 3D printing
or booleans. The colors are kept as materials"""

    bl_idname = "object.svgmesh_merge_solid"
    bl_label = "Merge into One Solid"
    bl_options = {"REGISTER", "UNDO"}

    keep_originals: BoolProperty(name="Keep Originals", default=False,
                                 description="Keep the separate objects (hidden) instead of deleting them")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and len(_meshes(context)) >= 2

    def execute(self, context):
        objs = _meshes(context)
        active = context.view_layer.objects.active
        if active in objs:  # the active object gives name and placement
            objs.remove(active)
            objs.insert(0, active)
        obj, open_edges = merge_solids(context, objs)
        for o in objs:
            if self.keep_originals:
                o.hide_set(True)
                o.select_set(False)
            else:
                me = o.data
                bpy.data.objects.remove(o)
                if me.users == 0:
                    bpy.data.meshes.remove(me)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        if open_edges:
            self.report({"WARNING"}, "Merged, but %d edge(s) are not closed - "
                        "Blender 4.5 or newer merges more reliably" % open_edges)
        else:
            self.report({"INFO"}, "Merged %d objects into one closed solid" % len(objs))
        return {"FINISHED"}


classes = (SVGMESH_OT_base_plate, SVGMESH_OT_merge_solid)
