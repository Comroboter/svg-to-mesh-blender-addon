"""Check for and install add-on updates from GitHub releases (only on request)."""

import os
import re
import sys

import bpy
from bpy.types import Operator

from .core import updater

# result of the last check, shown in the preferences and the sidebar
STATE = {}


def current_version():
    """Installed version: from the manifest (Blender removes bl_info from
    extensions), else from bl_info (legacy add-on in Blender 3.6 - 4.1)."""
    manifest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_manifest.toml")
    try:
        with open(manifest, encoding="utf-8") as fh:
            m = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
        version = updater.parse_version(m.group(1)) if m else None
        if version:
            return version
    except OSError:
        pass
    info = getattr(sys.modules.get(__package__), "bl_info", None) or {}
    return tuple(info.get("version", (0, 0, 0)))


def version_text(version):
    return ".".join(str(v) for v in version)


def online_allowed():
    return getattr(bpy.app, "online_access", True)


def _cursor(context, kind):
    if context.window is not None:  # None in background mode
        context.window.cursor_set(kind)


class SVGMESH_OT_check_update(Operator):
    """Ask GitHub whether a newer version of the add-on is available"""

    bl_idname = "preferences.svgmesh_check_update"
    bl_label = "Check for Updates"

    def execute(self, context):
        if not online_allowed():
            self.report({"ERROR"}, "Online access is disabled. Enable it in Preferences > System > Network")
            return {"CANCELLED"}
        _cursor(context, "WAIT")
        try:
            release = updater.latest_release()
        except updater.UpdateError as ex:
            STATE.clear()
            STATE.update(state="error", message=str(ex))
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        finally:
            _cursor(context, "DEFAULT")
        STATE.clear()
        if release["version"] > current_version():
            STATE.update(state="available", release=release)
            self.report({"INFO"}, "Version %s is available" % version_text(release["version"]))
        else:
            STATE.update(state="current")
            self.report({"INFO"}, "SVG to Clean Mesh is up to date (%s)" % version_text(current_version()))
        return {"FINISHED"}


class SVGMESH_OT_install_update(Operator):
    """Download the newest release from GitHub and install it (restart Blender afterwards)"""

    bl_idname = "preferences.svgmesh_install_update"
    bl_label = "Install Update"

    @classmethod
    def poll(cls, context):
        return STATE.get("state") == "available"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if not online_allowed():
            self.report({"ERROR"}, "Online access is disabled. Enable it in Preferences > System > Network")
            return {"CANCELLED"}
        release = STATE["release"]
        _cursor(context, "WAIT")
        try:
            data = updater.download(release)
            version = updater.install_zip(data, os.path.dirname(os.path.abspath(__file__)))
        except (updater.UpdateError, OSError) as ex:
            self.report({"ERROR"}, "Update failed: %s" % ex)
            return {"CANCELLED"}
        finally:
            _cursor(context, "DEFAULT")
        STATE.clear()
        STATE.update(state="installed", version=version)
        self.report({"INFO"}, "Updated to %s - restart Blender to use the new version" % version_text(version))
        return {"FINISHED"}


def draw_updates(layout, compact=False):
    """Update section for the preferences (and, compact, for the sidebar)."""
    state = STATE.get("state")
    if state == "available":
        release = STATE["release"]
        box = layout.box() if compact else layout
        box.label(text="Version %s is available" % version_text(release["version"]), icon="INFO")
        row = box.row(align=True)
        row.operator(SVGMESH_OT_install_update.bl_idname, icon="IMPORT")
        row.operator("wm.url_open", text="What's New", icon="URL").url = release["page"]
        return
    if state == "installed":
        layout.label(text="Updated to %s - restart Blender" % version_text(STATE["version"]), icon="CHECKMARK")
        return
    if compact:
        layout.operator(SVGMESH_OT_check_update.bl_idname, icon="URL")
        return
    row = layout.row()
    row.operator(SVGMESH_OT_check_update.bl_idname, icon="URL")
    if state == "current":
        row.label(text="Up to date (%s)" % version_text(current_version()), icon="CHECKMARK")
    elif state == "error":
        row.label(text=STATE["message"], icon="ERROR")
    else:
        row.label(text="Installed: %s" % version_text(current_version()))


classes = (SVGMESH_OT_check_update, SVGMESH_OT_install_update)
