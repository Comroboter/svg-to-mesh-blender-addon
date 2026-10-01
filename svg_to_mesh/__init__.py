"""SVG to Mesh – import SVGs and trace images (logos) directly into clean,
boolean-ready meshes."""

bl_info = {
    "name": "SVG to Clean Mesh",
    "author": "comroboter",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "File > Import, 3D Viewport > Sidebar > SVG Mesh",
    "description": "Import SVG files and trace images (logos) into clean, manifold meshes ready for booleans",
    "category": "Import-Export",
}

if "bpy" in locals():
    import importlib

    from . import core  # noqa: F401
    from .core import bezier_fit, geometry, svg_parser, svg_writer, tracer

    for _m in (geometry, bezier_fit, svg_parser, svg_writer, tracer, mesh_builder, pipeline, operators, ui):  # noqa: F821
        importlib.reload(_m)

import bpy  # noqa: E402

from . import mesh_builder, operators, pipeline, ui  # noqa: E402,F401


def register():
    for cls in operators.classes:
        bpy.utils.register_class(cls)
    ui.register()


def unregister():
    ui.unregister()
    for cls in reversed(operators.classes):
        bpy.utils.unregister_class(cls)
