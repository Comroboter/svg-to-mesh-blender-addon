import os
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper

from . import mesh_builder, pipeline
from .core.geometry import SubPath, VectorShape, line_segment
from .core.svg_parser import parse_svg
from .core.svg_writer import write_svg
from .core.tracer import TraceSettings, trace_image

# --------------------------------------------------------------------------
# Shared options
# --------------------------------------------------------------------------

TOPOLOGY_ITEMS = [
    ("NGON", "Clean N-Gons", "Minimal geometry: one n-gon cap per region, quad side walls. Best for booleans"),
    ("TRIS", "Triangles", "Constrained Delaunay triangulation of the outline (no extra vertices)"),
    ("UNIFORM", "Uniform Triangles", "Evenly sized triangles, good for deformation, displacement or cloth"),
    ("QUADS", "Quads", "Quad-dominant grid fill, good for subdivision and sculpting"),
]


class MeshOptions:
    topology: EnumProperty(name="Topology", items=TOPOLOGY_ITEMS, default="NGON")
    grid_size: FloatProperty(
        name="Grid Size", subtype="PERCENTAGE", default=2.0, min=0.2, max=50.0,
        description="Cell size of uniform triangles / quads, relative to the object size",
    )
    extrude: BoolProperty(name="Extrude", default=True, description="Create a closed, manifold solid")
    depth: FloatProperty(name="Depth", subtype="DISTANCE", unit="LENGTH", default=0.05, min=0.0, soft_max=1.0)
    center_depth: BoolProperty(
        name="Center Depth", default=False,
        description="Extrude symmetrically around Z=0 (handy for boolean cutters)",
    )
    curve_tolerance: FloatProperty(
        name="Curve Precision", subtype="PERCENTAGE", default=0.05, min=0.001, max=5.0, precision=3,
        description="Maximum deviation from the true curve, relative to the object size. "
        "Lower = rounder curves with more vertices",
    )
    separate: EnumProperty(
        name="Objects",
        items=[
            ("ONE", "Single Object", "Merge everything into one mesh"),
            ("COLOR", "Per Color", "One object per fill color (shared, gap-free borders)"),
            ("SHAPE", "Per Shape", "One object per SVG element / traced layer"),
        ],
        default="ONE",
    )
    overlap: EnumProperty(
        name="Overlaps",
        items=[
            ("VISIBLE", "Visible Only", "Shapes painted on top cut away what lies below them (like the SVG looks)"),
            ("UNION", "Union", "Keep every shape complete, overlaps are merged per object"),
        ],
        default="VISIBLE",
    )
    ignore_white: BoolProperty(
        name="White = Hole", default=True,
        description="White fills are treated as empty space (cut-outs, background rectangles)",
    )
    include_strokes: BoolProperty(name="Strokes", default=True, description="Convert outlines (strokes) to geometry")
    layer_offset: FloatProperty(
        name="Layer Offset", subtype="DISTANCE", unit="LENGTH", default=0.0,
        description="Z offset between separated objects (stacked, layered look)",
    )
    create_materials: BoolProperty(name="Materials", default=True, description="Create materials from the fill colors")

    def mesh_settings(self):
        return mesh_builder.MeshSettings(topology=self.topology, overlap=self.overlap)

    def import_settings(self, **kw):
        st = pipeline.ImportSettings(
            mesh=self.mesh_settings(),
            curve_tolerance=self.curve_tolerance / 100.0,
            grid_size=self.grid_size / 100.0,
            depth=self.depth if self.extrude else 0.0,
            separate=self.separate,
            ignore_white=self.ignore_white,
            include_strokes=self.include_strokes,
            layer_offset=self.layer_offset,
            create_materials=self.create_materials,
        )
        for k, v in kw.items():
            setattr(st, k, v)
        st.mesh.center_depth = self.center_depth
        return st

    def draw_mesh_options(self, layout, color_options=True):
        box = layout.box()
        box.label(text="Geometry", icon="MESH_DATA")
        box.prop(self, "topology", text="")
        if self.topology in ("UNIFORM", "QUADS"):
            box.prop(self, "grid_size")
        box.prop(self, "curve_tolerance")
        row = box.row(align=True)
        row.prop(self, "extrude")
        sub = row.row(align=True)
        sub.active = self.extrude
        sub.prop(self, "depth", text="")
        sub = box.row()
        sub.active = self.extrude
        sub.prop(self, "center_depth")
        if color_options:
            box = layout.box()
            box.label(text="Shapes & Colors", icon="COLOR")
            box.prop(self, "separate", text="")
            box.prop(self, "overlap", text="")
            row = box.row()
            row.prop(self, "ignore_white")
            row.prop(self, "include_strokes")
            box.prop(self, "create_materials")
            if self.separate != "ONE":
                box.prop(self, "layer_offset")


class SizeOptions:
    size_mode: EnumProperty(
        name="Size",
        items=[
            ("FIT", "Fit to Size", "Scale so that the largest side has the given length"),
            ("REAL", "Real Size", "Use the document size (SVG units / physical size)"),
        ],
        default="FIT",
    )
    target_size: FloatProperty(name="Max Size", subtype="DISTANCE", unit="LENGTH", default=1.0, min=1e-5)
    origin: EnumProperty(
        name="Origin",
        items=[("CENTER", "Center", ""), ("BOTTOM_LEFT", "Bottom Left", "")],
        default="CENTER",
    )

    def draw_size_options(self, layout, allow_real=True):
        box = layout.box()
        box.label(text="Size & Placement", icon="FULLSCREEN_ENTER")
        if allow_real:
            box.prop(self, "size_mode", text="")
        if self.size_mode == "FIT" or not allow_real:
            box.prop(self, "target_size")
        box.prop(self, "origin")


def _place_at_cursor(context, objects):
    loc = context.scene.cursor.location
    for o in objects:
        o.location = loc


def _new_collection(context, name):
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    return coll


class _FileImport(ImportHelper):
    files: CollectionProperty(type=OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})

    def paths(self):
        if self.files and self.directory:
            return [os.path.join(self.directory, f.name) for f in self.files if f.name]
        return [self.filepath]

    def invoke(self, context, event):
        if hasattr(ImportHelper, "invoke_popup"):  # Blender 4.1+: also handles drag & drop
            return self.invoke_popup(context)
        return ImportHelper.invoke(self, context, event)


# --------------------------------------------------------------------------
# SVG -> mesh
# --------------------------------------------------------------------------


class SVGMESH_OT_import_svg(Operator, _FileImport, MeshOptions, SizeOptions):
    """Import an SVG file directly as a clean mesh"""

    bl_idname = "import_mesh.svg_clean"
    bl_label = "Import SVG as Mesh"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".svg"
    filter_glob: StringProperty(default="*.svg", options={"HIDDEN"})

    def draw(self, context):
        layout = self.layout
        self.draw_size_options(layout)
        self.draw_mesh_options(layout)

    def execute(self, context):
        t0 = time.time()
        created = []
        for path in self.paths():
            try:
                doc = parse_svg(path)
            except Exception as ex:  # noqa: BLE001 - report any parse problem
                self.report({"ERROR"}, "Could not read %s: %s" % (os.path.basename(path), ex))
                continue
            for w in sorted(set(doc.warnings)):
                self.report({"WARNING"}, w)
            name = os.path.splitext(os.path.basename(path))[0]
            st = self.import_settings(
                scale_mode=self.size_mode,
                target_size=self.target_size,
                unit_scale=doc.mm_per_unit / 1000.0 / context.scene.unit_settings.scale_length,
                origin=self.origin,
            )
            coll = None
            if self.separate != "ONE":
                coll = _new_collection(context, name)
            objs = pipeline.build_objects(context, doc.shapes, st, name, collection=coll)
            if not objs:
                self.report({"WARNING"}, "%s: no filled shapes found" % os.path.basename(path))
                continue
            _place_at_cursor(context, objs)
            created.extend(objs)
        pipeline.select_objects(context, created)
        if not created:
            return {"CANCELLED"}
        self.report({"INFO"}, "Created %d object(s) in %.2fs" % (len(created), time.time() - t0))
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Image -> (SVG) -> mesh
# --------------------------------------------------------------------------


def load_image_rgba(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        ch = img.channels
        if w == 0 or h == 0:
            raise RuntimeError("image has no pixels")
        buf = np.empty(w * h * ch, dtype=np.float32)
        img.pixels.foreach_get(buf)
        return buf.reshape(h, w, ch)
    finally:
        bpy.data.images.remove(img)


class TraceOptions:
    trace_mode: EnumProperty(
        name="Mode",
        items=[
            ("AUTO", "Auto", "Transparency if the image has some, otherwise brightness"),
            ("BRIGHTNESS", "Brightness", "Dark vs. light (single color logo)"),
            ("ALPHA", "Transparency", "Opaque pixels become the shape"),
            ("COLORS", "Colors", "Separate the image into color regions"),
        ],
        default="AUTO",
    )
    auto_threshold: BoolProperty(name="Auto Threshold", default=True)
    threshold: FloatProperty(name="Threshold", default=0.5, min=0.0, max=1.0, subtype="FACTOR")
    invert: BoolProperty(name="Invert", default=False)
    num_colors: IntProperty(
        name="Colors", default=3, min=2, max=16,
        description="Number of colors including the background (unless the image is transparent)",
    )
    keep_background: BoolProperty(name="Keep Background", default=False)
    blur: FloatProperty(
        name="Edge Smoothing", default=0.8, min=0.0, max=5.0,
        description="Blur radius (pixels) applied before tracing; removes jaggies and noise",
    )
    smoothing: FloatProperty(
        name="Curve Smoothing", default=1.5, min=0.0, max=10.0,
        description="Smoothing of the traced outlines (pixels)",
    )
    fit_error: FloatProperty(
        name="Fit Tolerance", default=0.5, min=0.05, max=10.0,
        description="Maximum deviation of the fitted curves (pixels). Higher = fewer, smoother curves",
    )
    corner_angle: FloatProperty(
        name="Corner Angle", default=60.0, min=10.0, max=170.0,
        description="Direction changes sharper than this become corners",
    )
    despeckle: FloatProperty(
        name="Despeckle", default=12.0, min=0.0, max=10000.0,
        description="Remove specks and holes smaller than this area (pixels²)",
    )
    max_resolution: IntProperty(
        name="Max Resolution", default=2048, min=64, max=16384,
        description="Larger images are downsampled first (faster, less noise)",
    )
    save_svg: BoolProperty(name="Save SVG", default=False, description="Also write the traced vectors as SVG file")
    svg_path: StringProperty(
        name="SVG File", subtype="FILE_PATH", default="",
        description="Where to save the SVG (empty = <image name>_traced.svg next to the image). "
        "An existing file is overwritten",
    )

    def trace_settings(self):
        return TraceSettings(
            mode=self.trace_mode, threshold=self.threshold, auto_threshold=self.auto_threshold,
            invert=self.invert, num_colors=self.num_colors, blur=self.blur, despeckle=self.despeckle,
            corner_angle=self.corner_angle, fit_error=self.fit_error, smoothing=self.smoothing,
            max_resolution=self.max_resolution, keep_background=self.keep_background,
        )

    def draw_trace_options(self, layout):
        box = layout.box()
        box.label(text="Tracing", icon="IMAGE_DATA")
        box.prop(self, "trace_mode", text="")
        if self.trace_mode == "COLORS":
            box.prop(self, "num_colors")
            box.prop(self, "keep_background")
        else:
            row = box.row(align=True)
            row.prop(self, "auto_threshold", text="Auto")
            sub = row.row(align=True)
            sub.active = not self.auto_threshold
            sub.prop(self, "threshold")
            box.prop(self, "invert")
        col = box.column(align=True)
        col.prop(self, "blur")
        col.prop(self, "smoothing")
        col.prop(self, "fit_error")
        col.prop(self, "corner_angle")
        col.prop(self, "despeckle")
        box.prop(self, "max_resolution")
        row = box.row()
        row.prop(self, "save_svg")
        if self.save_svg:
            box.prop(self, "svg_path", text="")


class SVGMESH_OT_trace_image(Operator, _FileImport, TraceOptions, MeshOptions, SizeOptions):
    """Trace an image (e.g. a logo) into vector curves and build a clean mesh from it"""

    bl_idname = "import_mesh.image_trace"
    bl_label = "Trace Image to Mesh"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.bmp;*.tga;*.tif;*.tiff;*.webp;*.exr;*.hdr;*.gif",
        options={"HIDDEN"},
    )

    def draw(self, context):
        layout = self.layout
        self.draw_trace_options(layout)
        self.draw_size_options(layout, allow_real=False)
        self.draw_mesh_options(layout)

    def execute(self, context):
        t0 = time.time()
        created = []
        for path in self.paths():
            try:
                rgba = load_image_rgba(path)
            except Exception as ex:  # noqa: BLE001
                self.report({"ERROR"}, "Could not load %s: %s" % (os.path.basename(path), ex))
                continue
            shapes, w, h = trace_image(rgba, self.trace_settings())
            if not shapes:
                self.report({"WARNING"}, "%s: nothing to trace - try another mode or threshold" % os.path.basename(path))
                continue
            name = os.path.splitext(os.path.basename(path))[0]
            if self.save_svg:
                out = bpy.path.abspath(self.svg_path) if self.svg_path else os.path.splitext(path)[0] + "_traced.svg"
                if len(self.paths()) > 1 and self.svg_path:
                    out = os.path.join(os.path.dirname(out), name + "_traced.svg")
                try:
                    write_svg(out, shapes, w, h)
                    self.report({"INFO"}, "SVG saved: %s" % out)
                except OSError as ex:
                    self.report({"ERROR"}, "Could not write SVG: %s" % ex)
            st = self.import_settings(scale_mode="FIT", target_size=self.target_size, origin=self.origin)
            st.ignore_white = self.ignore_white and self.trace_mode == "COLORS"
            coll = _new_collection(context, name) if self.separate != "ONE" and len(shapes) > 1 else None
            objs = pipeline.build_objects(context, shapes, st, name, collection=coll)
            _place_at_cursor(context, objs)
            created.extend(objs)
        pipeline.select_objects(context, created)
        if not created:
            return {"CANCELLED"}
        self.report({"INFO"}, "Created %d object(s) in %.2fs" % (len(created), time.time() - t0))
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Existing curve / text objects -> mesh
# --------------------------------------------------------------------------


def curve_to_shapes(curve, name):
    """Read the closed splines of a Curve datablock as one even-odd shape."""
    subpaths = []
    for spline in curve.splines:
        if not spline.use_cyclic_u:
            continue
        if spline.type == "BEZIER":
            bp = spline.bezier_points
            n = len(bp)
            if n < 2:
                continue
            segs = []
            for i in range(n):
                a, b = bp[i], bp[(i + 1) % n]
                segs.append((
                    (a.co.x, a.co.y), (a.handle_right.x, a.handle_right.y),
                    (b.handle_left.x, b.handle_left.y), (b.co.x, b.co.y),
                ))
            subpaths.append(SubPath(segs, True))
        else:
            pts = [(p.co.x, p.co.y) for p in spline.points]
            if len(pts) < 3:
                continue
            segs = [line_segment(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
            subpaths.append(SubPath(segs, True))
    if not subpaths:
        return []
    return [VectorShape(subpaths=subpaths, fill=(0.0, 0.0, 0.0), fill_rule="evenodd", name=name)]


class SVGMESH_OT_curves_to_mesh(Operator, MeshOptions):
    """Convert selected curve and text objects (e.g. imported SVGs) into clean meshes"""

    bl_idname = "object.svgmesh_curves_to_mesh"
    bl_label = "Curves to Clean Mesh"
    bl_options = {"REGISTER", "UNDO"}

    keep_original: BoolProperty(name="Keep Original", default=False, description="Keep the curve objects (hidden)")
    use_material: BoolProperty(name="Keep Material", default=True, description="Copy the curve's first material")

    @classmethod
    def poll(cls, context):
        return any(o.type in {"CURVE", "FONT"} for o in context.selected_objects)

    def draw(self, context):
        layout = self.layout
        self.draw_mesh_options(layout, color_options=False)
        layout.prop(self, "keep_original")
        layout.prop(self, "use_material")

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        depsgraph = context.evaluated_depsgraph_get()
        created = []
        sources = [o for o in context.selected_objects if o.type in {"CURVE", "FONT"}]
        for obj in sources:
            obj_eval = obj.evaluated_get(depsgraph)
            curve = obj_eval.to_curve(depsgraph, apply_modifiers=False) if obj.type == "FONT" else obj.data
            try:
                shapes = curve_to_shapes(curve, obj.name)
            finally:
                if obj.type == "FONT":
                    obj_eval.to_curve_clear()
            if not shapes:
                self.report({"WARNING"}, "%s has no closed splines" % obj.name)
                continue
            st = self.import_settings(scale_mode="KEEP", origin="KEEP", separate="ONE", ignore_white=False)
            st.create_materials = False
            st.mesh.overlap = "UNION"
            # depth is given in world units; compensate the object scale
            sz = max(abs(obj.matrix_world.to_scale().z), 1e-9)
            st.depth = st.depth / sz
            colls = obj.users_collection
            objs = pipeline.build_objects(
                context, shapes, st, obj.name + "_mesh",
                collection=colls[0] if colls else None, matrix=obj.matrix_world.copy(),
            )
            if not objs:
                self.report({"WARNING"}, "%s: no filled area found - original kept" % obj.name)
                continue
            for o in objs:
                if self.use_material and obj.data.materials and obj.data.materials[0]:
                    o.data.materials.append(obj.data.materials[0])
            if not self.keep_original:
                bpy.data.objects.remove(obj, do_unlink=True)
            else:
                obj.hide_set(True)
            created.extend(objs)
        pipeline.select_objects(context, created)
        if not created:
            return {"CANCELLED"}
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Boolean helper
# --------------------------------------------------------------------------


class SVGMESH_OT_boolean(Operator):
    """Use the selected objects as boolean cutters on the active object"""

    bl_idname = "object.svgmesh_boolean"
    bl_label = "Boolean with Selection"
    bl_options = {"REGISTER", "UNDO"}

    operation: EnumProperty(
        name="Operation",
        items=[
            ("DIFFERENCE", "Engrave / Cut", "Subtract the selected objects"),
            ("UNION", "Emboss / Add", "Add the selected objects"),
            ("INTERSECT", "Intersect", "Keep only the overlap"),
        ],
        default="DIFFERENCE",
    )
    solver: EnumProperty(
        name="Solver",
        items=[("EXACT", "Exact", "Robust, handles coplanar faces"), ("FLOAT", "Fast", "Faster, less robust")],
        default="EXACT",
    )
    apply: BoolProperty(name="Apply Immediately", default=False, description="Apply the modifier and delete the cutters")
    hide_cutters: BoolProperty(name="Hide Cutters", default=True)

    @classmethod
    def poll(cls, context):
        act = context.active_object
        return act is not None and act.type == "MESH" and any(
            o is not act and o.type == "MESH" for o in context.selected_objects
        )

    def execute(self, context):
        target = context.active_object
        cutters = [o for o in context.selected_objects if o is not target and o.type == "MESH"]
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for cutter in cutters:
            mod = target.modifiers.new(name="SVG " + cutter.name, type="BOOLEAN")
            mod.operation = self.operation
            mod.object = cutter
            try:
                mod.solver = self.solver
            except TypeError:
                pass
            if self.apply:
                with context.temp_override(object=target, active_object=target):
                    bpy.ops.object.modifier_apply(modifier=mod.name)
            else:
                cutter.display_type = "WIRE"
                cutter.hide_render = True
                if self.hide_cutters:
                    cutter.hide_set(True)
        if self.apply:
            for cutter in cutters:
                bpy.data.objects.remove(cutter, do_unlink=True)
        for o in context.selected_objects:
            o.select_set(False)
        target.select_set(True)
        context.view_layer.objects.active = target
        return {"FINISHED"}


classes = (
    SVGMESH_OT_import_svg,
    SVGMESH_OT_trace_image,
    SVGMESH_OT_curves_to_mesh,
    SVGMESH_OT_boolean,
)
