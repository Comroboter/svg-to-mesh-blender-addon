"""Pure-Python core of the add-on.

Nothing in this package imports ``bpy`` so it can be unit-tested outside of
Blender.  Only :mod:`.tracer` and :mod:`.bezier_fit` need numpy (which ships with Blender).
"""
