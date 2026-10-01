import bpy

from .operators import (
    SVGMESH_OT_boolean,
    SVGMESH_OT_curves_to_mesh,
    SVGMESH_OT_import_svg,
    SVGMESH_OT_trace_image,
)


class SVGMESH_PT_panel(bpy.types.Panel):
    bl_label = "SVG to Mesh"
    bl_idname = "SVGMESH_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SVG Mesh"

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
        layout.label(text="Tip: F9 adjusts the last import", icon="INFO")


def menu_import(self, context):
    self.layout.operator(SVGMESH_OT_import_svg.bl_idname, text="SVG as Clean Mesh (.svg)")
    self.layout.operator(SVGMESH_OT_trace_image.bl_idname, text="Image Trace to Mesh")


def menu_convert(self, context):
    self.layout.operator(SVGMESH_OT_curves_to_mesh.bl_idname, text="Clean Mesh (from Curve/Text)")


classes = [SVGMESH_PT_panel]

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
