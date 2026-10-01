"""Operators of the "Depth per Object" sidebar section."""

import math
import threading
import time

import bpy
from bpy.props import FloatProperty, StringProperty
from bpy.types import Operator

from . import depth_tools, prefs
from .core import ai_client


def online_allowed():
    """Blender 4.2+ lets users forbid network access for add-ons."""
    return getattr(bpy.app, "online_access", True)


BASE_DEPTH_DESC = ("Thickness that height 1.0 corresponds to. "
                   "0 = automatic: the current thickness of the selected objects")


def _base_depth(op, objs):
    """Resolve the operator's base depth (0 = automatic) and report a warning if needed."""
    value, warning = depth_tools.resolve_base_depth(objs, op.base_depth)
    if warning:
        op.report({"WARNING"}, warning)
    return value


# the running interactive AI request (shown as a progress bar in the sidebar)
AI_JOB = {}
AI_EXPECTED_SECONDS = 25.0  # typical answer time, only shapes the progress bar
AI_STALE_SECONDS = 900.0


def ai_job_status(now=None):
    """(elapsed seconds, progress 0..1) of the running AI request, or None.

    The API gives no progress, so the bar approaches 95 % with the typical
    answer time and the elapsed seconds show that something is happening.
    """
    start = AI_JOB.get("start")
    if start is None:
        return None
    elapsed = max(0.0, (time.time() if now is None else now) - start)
    if elapsed > AI_STALE_SECONDS:  # the operator was killed without cleaning up
        AI_JOB.clear()
        return None
    return elapsed, min(0.95, 1.0 - math.exp(-elapsed / AI_EXPECTED_SECONDS))


def ai_job_text(elapsed):
    if elapsed < 2.0:
        return "Sending preview to Claude..."
    return "Claude is thinking... %d s" % elapsed


def _redraw_sidebars(context):
    screen = getattr(context, "screen", None)
    for area in screen.areas if screen is not None else ():
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _selected_meshes(context):
    return [o for o in context.selected_objects if o.type == "MESH" and o.data.vertices]


class SVGMESH_OT_terrace(Operator):
    """Give every selected object its own height: lower layers flat, upper layers higher (no AI)"""

    bl_idname = "object.svgmesh_terrace"
    bl_label = "Terrace by Order"
    bl_options = {"REGISTER", "UNDO"}

    base_depth: FloatProperty(name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0,
                              description=BASE_DEPTH_DESC)
    step: FloatProperty(name="Step", default=0.7, min=0.0, max=10.0,
                        description="Extra height per layer, in multiples of the base depth")

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context))

    def invoke(self, context, event):
        self.base_depth = context.scene.svgmesh_depth_unit
        return self.execute(context)

    def execute(self, context):
        ordered = depth_tools.paint_order(_selected_meshes(context))
        depth_tools.terrace(ordered, _base_depth(self, ordered), self.step)
        return {"FINISHED"}


class SVGMESH_OT_reapply_depth(Operator):
    """Apply the stored heights of the selected objects again with the current base depth"""

    bl_idname = "object.svgmesh_reapply_depth"
    bl_label = "Re-apply with Base Depth"
    bl_options = {"REGISTER", "UNDO"}

    base_depth: FloatProperty(name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0,
                              description=BASE_DEPTH_DESC)

    @classmethod
    def poll(cls, context):
        return any("svgmesh_height" in o for o in _selected_meshes(context))

    def invoke(self, context, event):
        self.base_depth = context.scene.svgmesh_depth_unit
        return self.execute(context)

    def execute(self, context):
        objs = [o for o in _selected_meshes(context) if "svgmesh_height" in o]
        heights = {o: (o["svgmesh_height"], o.get("svgmesh_base", 0.0), o.get("svgmesh_reason", "")) for o in objs}
        depth_tools.apply_heights(objs, heights, _base_depth(self, objs))
        return {"FINISHED"}


class SVGMESH_OT_ai_depth(Operator):
    """Ask Claude to suggest a height for every selected object (sends a small preview image)"""

    bl_idname = "object.svgmesh_ai_depth"
    bl_label = "Suggest with AI"
    bl_options = {"UNDO"}

    base_depth: FloatProperty(name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0,
                              description=BASE_DEPTH_DESC)
    hint: StringProperty(name="Hint", default="")

    _timer = None
    _thread = None
    _result = None

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context)) and ai_job_status() is None

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
        depth_tools.apply_heights(ordered, heights, _base_depth(self, ordered))
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
        self.base_depth = context.scene.svgmesh_depth_unit
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
        AI_JOB.clear()
        AI_JOB["start"] = time.time()
        self._update_status(context)
        return {"RUNNING_MODAL"}

    def _update_status(self, context):
        status = ai_job_status()
        if status is not None and context.workspace is not None:
            context.workspace.status_text_set("%s  (Esc to cancel)" % ai_job_text(status[0]))
        _redraw_sidebars(context)

    def _cleanup(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        AI_JOB.clear()
        if context.workspace is not None:
            context.workspace.status_text_set(None)
        _redraw_sidebars(context)

    def cancel(self, context):  # Blender ends the operator (new file, window closed)
        self._cleanup(context)

    def modal(self, context, event):
        if event.type == "ESC" or AI_JOB.get("cancel"):
            self._cleanup(context)
            self.report({"INFO"}, "AI request cancelled")
            return {"CANCELLED"}
        if event.type == "TIMER" and self._thread.is_alive():
            self._update_status(context)
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


class SVGMESH_OT_ai_cancel(Operator):
    """Stop waiting for the AI suggestions (nothing is changed)"""

    bl_idname = "object.svgmesh_ai_cancel"
    bl_label = "Cancel"

    def execute(self, context):
        if ai_job_status() is not None:
            AI_JOB["cancel"] = True
        return {"FINISHED"}


classes = (SVGMESH_OT_terrace, SVGMESH_OT_reapply_depth, SVGMESH_OT_ai_depth, SVGMESH_OT_ai_cancel)


def register_props():
    # (renamed from svgmesh_base_depth: old scenes may hold a fixed, too small value)
    bpy.types.Scene.svgmesh_depth_unit = FloatProperty(
        name="Base Depth", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0,
        description=BASE_DEPTH_DESC,
    )
    bpy.types.Scene.svgmesh_ai_hint = StringProperty(
        name="Hint", default="",
        description="Optional: what you want to make, e.g. 'keychain', 'wall sign', 'stamp'",
    )


def unregister_props():
    del bpy.types.Scene.svgmesh_ai_hint
    del bpy.types.Scene.svgmesh_depth_unit
