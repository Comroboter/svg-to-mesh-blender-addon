"""SVG to Mesh – import SVGs and trace images (logos) directly into clean,
boolean-ready meshes."""

bl_info = {
    "name": "SVG to Clean Mesh",
    "author": "comroboter",
    "version": (1, 2, 1),
    "blender": (3, 6, 0),
    "location": "File > Import, 3D Viewport > Sidebar > SVG Mesh",
    "description": "Import SVG files and trace images (logos) into clean, manifold meshes ready for booleans",
    "category": "Import-Export",
}

if "bpy" in locals():
    import importlib

    from . import core  # noqa: F401
    from .core import ai_client, bezier_fit, geometry, raster, svg_parser, svg_writer, tracer

    for _m in (geometry, bezier_fit, svg_parser, svg_writer, tracer, raster, ai_client, mesh_builder,  # noqa: F821
               pipeline, depth_tools, prefs, operators, depth_ops, ui):  # noqa: F821
        importlib.reload(_m)

import bpy  # noqa: E402

from . import depth_ops, depth_tools, mesh_builder, operators, pipeline, prefs, ui  # noqa: E402,F401

_CLASSES = prefs.classes + operators.classes + depth_ops.classes


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    depth_ops.register_props()
    ui.register()


def unregister():
    ui.unregister()
    depth_ops.unregister_props()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
