import os
import textwrap

import bpy

from . import depth_ops, prefs, update_ops
from .depth_ops import (
    SVGMESH_OT_ai_cancel,
    SVGMESH_OT_ai_depth,
    SVGMESH_OT_reapply_depth,
    SVGMESH_OT_split_parts,
    SVGMESH_OT_terrace,
)
from .finish import SVGMESH_OT_base_plate, SVGMESH_OT_merge_solid
from .operators import (
    SVGMESH_OT_boolean,
    SVGMESH_OT_curves_to_mesh,
    SVGMESH_OT_import_svg,
    SVGMESH_OT_trace_image,
)


_previews = None


def logo_icon():
    """icon_value of the add-on logo (0 = no icon) for layout calls."""
    try:
        return _previews["logo"].icon_id
    except (TypeError, KeyError):
        return 0


class SVGMESH_PT_panel(bpy.types.Panel):
    bl_label = "SVG to Mesh"
    bl_idname = "SVGMESH_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SVG Mesh"

    def draw_header(self, context):
        self.layout.label(text="", icon_value=logo_icon())

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Import")
        col.operator(SVGMESH_OT_import_svg.bl_idname, icon="CURVE_BEZCURVE")
        col.operator(SVGMESH_OT_trace_image.bl_idname, icon="IMAGE_DATA")

        col = layout.column(align=True)
        col.label(text="Convert")
        col.operator(SVGMESH_OT_curves_to_mesh.bl_idname, icon="OUTLINER_OB_MESH")

        col = layout.column(align=True)
        col.label(text="Boolean (active = target)")
        row = col.row(align=True)
        op = row.operator(SVGMESH_OT_boolean.bl_idname, text="Cut", icon="SELECT_SUBTRACT")
        op.operation = "DIFFERENCE"
        op = row.operator(SVGMESH_OT_boolean.bl_idname, text="Add", icon="SELECT_EXTEND")
        op.operation = "UNION"
        col = layout.column(align=True)
        col.label(text="Finish")
        col.operator(SVGMESH_OT_base_plate.bl_idname, icon="MESH_PLANE")
        col.operator(SVGMESH_OT_merge_solid.bl_idname, icon="AUTOMERGE_ON")
        layout.label(text="Tip: F9 adjusts the last import", icon="INFO")
        update_ops.draw_updates(layout, compact=True)


class SVGMESH_PT_depth(bpy.types.Panel):
    bl_label = "Depth per Object"
    bl_idname = "SVGMESH_PT_depth"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SVG Mesh"
    bl_parent_id = "SVGMESH_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "svgmesh_depth_unit", text="Base Depth (0 = auto)")
        layout.prop(scene, "svgmesh_flat_bottom")
        row = layout.row(align=True)
        row.operator(SVGMESH_OT_terrace.bl_idname, icon="SORTSIZE")
        row.operator(SVGMESH_OT_reapply_depth.bl_idname, text="", icon="FILE_REFRESH")
        layout.operator(SVGMESH_OT_split_parts.bl_idname, icon="MOD_EXPLODE")

        box = layout.box()
        box.label(text="AI suggestions (optional)")
        if not prefs.ai_ready(context):
            col = box.column(align=True)
            col.label(text="Let an AI pick a height per color")
            col.label(text="based on what the logo shows.")
            box.operator("preferences.addon_show", text="Set up AI...", icon="PREFERENCES").module = __package__
        elif depth_ops.ai_job_status() is not None:
            draw_ai_progress(box, *depth_ops.ai_job_status())
        else:
            box.prop(scene, "svgmesh_ai_hint", text="Hint")
            box.prop(scene, "svgmesh_ai_split")
            box.operator(SVGMESH_OT_ai_depth.bl_idname, icon="SHADERFX")
        obj = context.active_object
        reason = obj.get("svgmesh_reason") if obj is not None else None
        if reason:
            col = box.column(align=True)
            col.label(text="%s: height %.2g" % (obj.name, obj.get("svgmesh_height", 0)))
            for line in textwrap.wrap(str(reason), 36):
                col.label(text=line)


def draw_ai_progress(layout, elapsed, factor):
    text = depth_ops.ai_job_text(elapsed)
    row = layout.row(align=True)
    if hasattr(row, "progress"):  # Blender 4.0+
        row.progress(factor=factor, type="BAR", text=text)
    else:
        row.label(text=text, icon="SORTTIME")
    row.operator(SVGMESH_OT_ai_cancel.bl_idname, text="", icon="CANCEL")


def menu_import(self, context):
    self.layout.operator(SVGMESH_OT_import_svg.bl_idname, text="SVG as Clean Mesh (.svg)", icon_value=logo_icon())
    self.layout.operator(SVGMESH_OT_trace_image.bl_idname, text="Image Trace to Mesh", icon_value=logo_icon())


def menu_convert(self, context):
    self.layout.operator(SVGMESH_OT_curves_to_mesh.bl_idname, text="Clean Mesh (from Curve/Text)")


classes = [SVGMESH_PT_panel, SVGMESH_PT_depth]

if hasattr(bpy.types, "FileHandler"):  # Blender 4.1+: drag & drop .svg files

    class SVGMESH_FH_svg(bpy.types.FileHandler):
        bl_idname = "SVGMESH_FH_svg"
        bl_label = "SVG as Clean Mesh"
        bl_import_operator = SVGMESH_OT_import_svg.bl_idname
        bl_file_extensions = ".svg"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type == "VIEW_3D"

    classes.append(SVGMESH_FH_svg)


def register():
    global _previews
    try:
        import bpy.utils.previews

        _previews = bpy.utils.previews.new()
        _previews.load("logo", os.path.join(os.path.dirname(__file__), "icons", "logo.png"), "IMAGE")
    except Exception:  # noqa: BLE001 - the icon is cosmetic
        _previews = None
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    if hasattr(bpy.types, "VIEW3D_MT_object_convert"):
        bpy.types.VIEW3D_MT_object_convert.append(menu_convert)


def unregister():
    if hasattr(bpy.types, "VIEW3D_MT_object_convert"):
        bpy.types.VIEW3D_MT_object_convert.remove(menu_convert)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
