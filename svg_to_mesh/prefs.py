import os

import bpy
from bpy.props import EnumProperty, StringProperty

from .core.ai_client import DEFAULT_MODEL, DEFAULT_MODELS, DEFAULT_URLS, MODELS, PROVIDERS

API_KEY_URL = "https://platform.claude.com"
OPENAI_KEY_URL = "https://platform.openai.com/api-keys"
OLLAMA_URL = "https://ollama.com"


class SVGMESH_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    provider: EnumProperty(name="AI Service", items=PROVIDERS, default="ANTHROPIC")
    api_key: StringProperty(
        name="Anthropic API Key", subtype="PASSWORD", default="",
        description="Only needed for the optional AI depth suggestions. "
        "Leave empty to use the ANTHROPIC_API_KEY environment variable",
    )
    model: EnumProperty(name="Model", items=MODELS, default=DEFAULT_MODEL)
    openai_key: StringProperty(
        name="API Key", subtype="PASSWORD", default="",
        description="OpenAI API key (or the key of the compatible service). "
        "Leave empty to use the OPENAI_API_KEY environment variable; local servers usually need none",
    )
    openai_url: StringProperty(
        name="Server", default=DEFAULT_URLS["OPENAI"],
        description="Base URL of the API, e.g. https://api.openai.com/v1, http://localhost:1234/v1 (LM Studio) "
        "or https://openrouter.ai/api/v1",
    )
    openai_model: StringProperty(
        name="Model", default=DEFAULT_MODELS["OPENAI"],
        description="Model name; it must accept images (vision)",
    )
    ollama_url: StringProperty(name="Server", default=DEFAULT_URLS["OLLAMA"], description="Address of the Ollama server")
    ollama_model: StringProperty(
        name="Model", default=DEFAULT_MODELS["OLLAMA"],
        description="A vision model you have downloaded with 'ollama pull', e.g. gemma3, qwen2.5vl or llama3.2-vision",
    )

    def draw(self, context):
        from . import update_ops

        layout = self.layout
        box = layout.box()
        box.label(text="Updates")
        update_ops.draw_updates(box)
        box.label(text="Downloads the newest release from GitHub. Nothing is checked automatically.")

        box = layout.box()
        box.label(text="Optional: AI depth suggestions")
        col = box.column(align=True)
        col.label(text="Lets an AI suggest how high each color or part is extruded.")
        col.label(text="Nothing is sent unless you press 'Suggest with AI' in the sidebar (SVG Mesh > Depth per Object).")
        box.prop(self, "provider")
        if self.provider == "ANTHROPIC":
            box.prop(self, "api_key")
            if not self.api_key and os.environ.get("ANTHROPIC_API_KEY"):
                box.label(text="Using ANTHROPIC_API_KEY from the environment.")
            box.prop(self, "model")
            box.operator("wm.url_open", text="Get an API key (platform.claude.com)").url = API_KEY_URL
            col = box.column(align=True)
            col.label(text="Billed to your Anthropic account. Estimated cost per request: Opus 5.5 about 3-8 cents,")
            col.label(text="Sonnet 5.5 about 2-4 cents, Haiku 4.5 under 1 cent (more parts = a bit more).")
        elif self.provider == "OPENAI":
            box.prop(self, "openai_url")
            box.prop(self, "openai_key")
            if not self.openai_key and os.environ.get("OPENAI_API_KEY"):
                box.label(text="Using OPENAI_API_KEY from the environment.")
            box.prop(self, "openai_model")
            box.operator("wm.url_open", text="Get an API key (platform.openai.com)").url = OPENAI_KEY_URL
            box.label(text="Also works with LM Studio, OpenRouter and other OpenAI-compatible servers.")
        else:
            box.prop(self, "ollama_url")
            box.prop(self, "ollama_model")
            col = box.column(align=True)
            col.label(text="Free and private: runs on your own computer. Install Ollama, then run")
            col.label(text="'ollama pull %s' once. Small local models judge less well than Claude." % self.ollama_model)
            box.operator("wm.url_open", text="Get Ollama (ollama.com)").url = OLLAMA_URL
        col = box.column(align=True)
        col.label(text="Sent per request: two small preview images of the selected objects,")
        col.label(text="their colors, sizes and names, and your optional hint.")
        if not getattr(bpy.app, "online_access", True):
            box.label(text="Online access is disabled: Preferences > System > Network.", icon="ERROR")


def get_prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_provider(context):
    prefs = get_prefs(context)
    return prefs.provider if prefs else "ANTHROPIC"


def get_api_key(context):
    """API key for the selected AI service ("" if none is needed or set)."""
    prefs = get_prefs(context)
    provider = prefs.provider if prefs else "ANTHROPIC"
    if provider == "OLLAMA":
        return ""
    if provider == "OPENAI":
        key = prefs.openai_key.strip() if prefs else ""
        return key or os.environ.get("OPENAI_API_KEY", "").strip()
    key = prefs.api_key.strip() if prefs else ""
    return key or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def get_model(context):
    prefs = get_prefs(context)
    if prefs is None:
        return DEFAULT_MODEL
    return {"OPENAI": prefs.openai_model, "OLLAMA": prefs.ollama_model}.get(prefs.provider, prefs.model).strip()


def get_server(context):
    prefs = get_prefs(context)
    if prefs is None:
        return ""
    return {"OPENAI": prefs.openai_url, "OLLAMA": prefs.ollama_url}.get(prefs.provider, "").strip()


def ai_ready(context):
    """True if the selected AI service is set up (Ollama and custom servers need no key)."""
    provider = get_provider(context)
    if provider == "OLLAMA":
        return bool(get_model(context))
    if provider == "OPENAI" and get_server(context).rstrip("/") != DEFAULT_URLS["OPENAI"]:
        return bool(get_model(context))
    return bool(get_api_key(context))


classes = (SVGMESH_AddonPreferences,)
