import os

import bpy
from bpy.props import EnumProperty, StringProperty

from .core.ai_client import DEFAULT_MODEL, MODELS

API_KEY_URL = "https://platform.claude.com"


class SVGMESH_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    api_key: StringProperty(
        name="Anthropic API Key", subtype="PASSWORD", default="",
        description="Only needed for the optional AI depth suggestions. "
        "Leave empty to use the ANTHROPIC_API_KEY environment variable",
    )
    model: EnumProperty(name="Model", items=MODELS, default=DEFAULT_MODEL)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Optional: AI depth suggestions")
        col = box.column(align=True)
        col.label(text="Lets Claude (Anthropic) suggest how high each color or object is extruded.")
        col.label(text="Nothing is sent unless you press 'Suggest with AI' in the sidebar (SVG Mesh > Depth per Object).")
        box.prop(self, "api_key")
        if not self.api_key and os.environ.get("ANTHROPIC_API_KEY"):
            box.label(text="Using ANTHROPIC_API_KEY from the environment.")
        box.prop(self, "model")
        box.operator("wm.url_open", text="Get an API key (platform.claude.com)").url = API_KEY_URL
        col = box.column(align=True)
        col.label(text="Sent per request: a small preview image of the selected objects,")
        col.label(text="their colors, sizes and names, and your optional hint.")
        col.label(text="Usage is billed to your Anthropic account (typically a few cents per request).")
        if not getattr(bpy.app, "online_access", True):
            box.label(text="Online access is disabled: Preferences > System > Network.", icon="ERROR")


def get_prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_api_key(context):
    prefs = get_prefs(context)
    key = prefs.api_key.strip() if prefs else ""
    return key or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def get_model(context):
    prefs = get_prefs(context)
    return prefs.model if prefs else DEFAULT_MODEL


classes = (SVGMESH_AddonPreferences,)
