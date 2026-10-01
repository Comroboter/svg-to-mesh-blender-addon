"""Operators of the "Depth per Object" sidebar section."""

import threading

import bpy
from bpy.props import FloatProperty, StringProperty
from bpy.types import Operator

from . import depth_tools, prefs
from .core import ai_client


def online_allowed():
    """Blender 4.2+ lets users forbid network access for add-ons."""
    return getattr(bpy.app, "online_access", True)


def _selected_meshes(context):
    return [o for o in context.selected_objects if o.type == "MESH" and o.data.vertices]


class SVGMESH_OT_terrace(Operator):
    """Give every selected object its own height: lower layers flat, upper layers higher (no AI)"""

    bl_idname = "object.svgmesh_terrace"
    bl_label = "Terrace by Order"
    bl_options = {"REGISTER", "UNDO"}

    base_depth: FloatProperty(name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.02, min=1e-5)
    step: FloatProperty(name="Step", default=0.7, min=0.0, max=10.0,
                        description="Extra height per layer, in multiples of the base depth")

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context))

    def invoke(self, context, event):
        self.base_depth = context.scene.svgmesh_base_depth
        return self.execute(context)

    def execute(self, context):
        ordered = depth_tools.paint_order(_selected_meshes(context))
        depth_tools.terrace(ordered, self.base_depth, self.step)
        return {"FINISHED"}


class SVGMESH_OT_reapply_depth(Operator):
    """Apply the stored heights of the selected objects again with the current base depth"""

    bl_idname = "object.svgmesh_reapply_depth"
    bl_label = "Re-apply with Base Depth"
    bl_options = {"REGISTER", "UNDO"}

    base_depth: FloatProperty(name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.02, min=1e-5)

    @classmethod
    def poll(cls, context):
        return any("svgmesh_height" in o for o in _selected_meshes(context))

    def invoke(self, context, event):
        self.base_depth = context.scene.svgmesh_base_depth
        return self.execute(context)

    def execute(self, context):
        objs = [o for o in _selected_meshes(context) if "svgmesh_height" in o]
        heights = {o: (o["svgmesh_height"], o.get("svgmesh_base", 0.0), o.get("svgmesh_reason", "")) for o in objs}
        depth_tools.apply_heights(objs, heights, self.base_depth)
        return {"FINISHED"}


class SVGMESH_OT_ai_depth(Operator):
    """Ask Claude to suggest a height for every selected object (sends a small preview image)"""

    bl_idname = "object.svgmesh_ai_depth"
    bl_label = "Suggest with AI"
    bl_options = {"UNDO"}

    base_depth: FloatProperty(name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.02, min=1e-5)
    hint: StringProperty(name="Hint", default="")

    _timer = None
    _thread = None
    _result = None

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context))

    # -- preparation (main thread) --------------------------------------
    def _prepare(self, context):
        if not online_allowed():
            self.report({"ERROR"}, "Online access is disabled. Enable it in Preferences > System > Network")
            return None
        key = prefs.get_api_key(context)
        if not key:
            self.report({"ERROR"}, "No API key - add one in the add-on preferences")
            return None
        objs = _selected_meshes(context)
        if len(objs) < 2:
            self.report({"WARNING"}, "Select at least two objects (import with Objects: Per Color or Per Shape)")
            return None
        regions, png, ordered = depth_tools.collect_regions(objs)
        headers, body = ai_client.build_request(regions, png, self.hint, prefs.get_model(context))
        return key, headers, body, ordered

    def _finish(self, context, data, ordered):
        try:
            suggestions, summary = ai_client.parse_response(data, set(range(1, len(ordered) + 1)))
        except ai_client.AIError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        heights = {ordered[rid - 1]: value for rid, value in suggestions.items()}
        depth_tools.apply_heights(ordered, heights, self.base_depth)
        missing = len(ordered) - len(heights)
        msg = summary or "Heights applied"
        if missing:
            msg += " (%d object(s) left unchanged)" % missing
        self.report({"INFO"}, msg)
        return {"FINISHED"}

    # -- blocking run (scripts, background mode) ------------------------
    def execute(self, context):
        prepared = self._prepare(context)
        if prepared is None:
            return {"CANCELLED"}
        key, headers, body, ordered = prepared
        try:
            data = ai_client.call_api(key, headers, body)
        except ai_client.AIError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        return self._finish(context, data, ordered)

    # -- interactive run: the request runs in a thread, Blender stays responsive
    def invoke(self, context, event):
        self.base_depth = context.scene.svgmesh_base_depth
        self.hint = context.scene.svgmesh_ai_hint
        prepared = self._prepare(context)
        if prepared is None:
            return {"CANCELLED"}
        key, headers, body, self._ordered = prepared
        self._result = {}

        def work(result=self._result):
            try:
                result["data"] = ai_client.call_api(key, headers, body)
            except ai_client.AIError as ex:
                result["error"] = str(ex)
            except Exception as ex:  # noqa: BLE001 - surface anything unexpected to the user
                result["error"] = "Unexpected error: %s" % ex

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set("Asking Claude for depth suggestions...  (Esc to cancel)")
        return {"RUNNING_MODAL"}

    def _cleanup(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        context.workspace.status_text_set(None)

    def modal(self, context, event):
        if event.type == "ESC":
            self._cleanup(context)
            self.report({"INFO"}, "AI request cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER" or self._thread.is_alive():
            return {"PASS_THROUGH"}
        self._cleanup(context)
        if "error" in self._result:
            self.report({"ERROR"}, self._result["error"])
            return {"CANCELLED"}
        try:
            alive = all(o.name in bpy.data.objects for o in self._ordered)
        except ReferenceError:  # an object was deleted meanwhile
            alive = False
        if not alive:
            self.report({"WARNING"}, "Objects changed while waiting - nothing applied")
            return {"CANCELLED"}
        return self._finish(context, self._result["data"], self._ordered)


classes = (SVGMESH_OT_terrace, SVGMESH_OT_reapply_depth, SVGMESH_OT_ai_depth)


def register_props():
    bpy.types.Scene.svgmesh_base_depth = FloatProperty(
        name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.02, min=1e-5,
        description="Thickness that height 1.0 corresponds to",
    )
    bpy.types.Scene.svgmesh_ai_hint = StringProperty(
        name="Hint", default="",
        description="Optional: what you want to make, e.g. 'keychain', 'wall sign', 'stamp'",
    )


def unregister_props():
    del bpy.types.Scene.svgmesh_ai_hint
    del bpy.types.Scene.svgmesh_base_depth
