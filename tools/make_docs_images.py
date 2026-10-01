"""Regenerate all images in docs/.

Needs Blender as a Python module plus matplotlib::

    pip install bpy==5.0.1 matplotlib numpy
    python tools/make_docs_images.py            # everything
    python tools/make_docs_images.py topologies # only some images

3D renders use Cycles on the CPU and take a few minutes each.
"""

import math
import os
import sys

import bpy
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import svg_to_mesh  # noqa: E402
from svg_to_mesh import pipeline  # noqa: E402
from svg_to_mesh.core.geometry import flatten_subpath  # noqa: E402
from svg_to_mesh.core.tracer import TraceSettings, marching_squares, trace_image  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
EXAMPLES = os.path.join(ROOT, "examples")
TMP = os.path.join(ROOT, "dist", "docs_tmp")

NAVY = "#1d3557"
RED = "#e63946"
FILL = "#dfe7f2"
plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.titlecolor": NAVY})


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def clear_scene():
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.lights,
                 bpy.data.cameras):
        for block in list(coll):
            coll.remove(block)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)


def linear_to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def obj_color(obj, default=FILL):
    mats = obj.data.materials
    if mats and mats[0]:
        return tuple(linear_to_srgb(c) for c in mats[0].diffuse_color[:3])
    return default


def top_polys(obj):
    """Polygons of faces pointing up (the visible cap), in world XY."""
    mw = obj.matrix_world
    me = obj.data
    out = []
    for p in me.polygons:
        if abs(p.normal.z) > 0.5 and (p.normal.z > 0 or all(abs(me.vertices[i].co.z) < 1e-9 for i in p.vertices)):
            out.append([(mw @ me.vertices[i].co).xy[:] for i in p.vertices])
    return out


def mesh_stats(objs):
    v = sum(len(o.data.vertices) for o in objs)
    f = sum(len(o.data.polygons) for o in objs)
    tris = 0
    for o in objs:
        o.data.calc_loop_triangles()
        tris += len(o.data.loop_triangles)
    return v, f, tris


def capture(objs):
    """Snapshot of objects (polygons, colors, stats) that survives clear_scene()."""
    edges = []
    for o in objs:
        if not o.data.polygons:  # wire-only result: keep the edges
            mw = o.matrix_world
            vs = o.data.vertices
            edges += [[(mw @ vs[a].co).xy[:], (mw @ vs[b].co).xy[:]] for a, b in (e.vertices for e in o.data.edges)]
    return {"parts": [(top_polys(o), obj_color(o)) for o in objs], "edges": edges, "stats": mesh_stats(objs)}


def draw_objs(ax, snap, title, colored=True, lw=0.35, edge=NAVY):
    for polys, color in snap["parts"]:
        col = color if colored else FILL
        ax.add_collection(PolyCollection(polys, facecolors=[col], edgecolors=edge, linewidths=lw))
    if snap.get("edges"):
        ax.add_collection(LineCollection(snap["edges"], colors=edge, linewidths=1.0))
    ax.autoscale()
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(title)


def save(fig, name):
    path = os.path.join(DOCS, name)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=110, facecolor="white")
    plt.close(fig)
    print("wrote", path)


def mesh_objects():
    return [o for o in bpy.data.objects if o.type == "MESH"]


def import_svg(path, **kw):
    for o in list(bpy.context.selected_objects):
        o.select_set(False)
    res = bpy.ops.import_mesh.svg_clean(filepath=path, **kw)
    assert res == {"FINISHED"}, res
    return list(bpy.context.selected_objects)


# --------------------------------------------------------------------------
# 2D figures
# --------------------------------------------------------------------------


def fig_compare_svg():
    """Blender's built-in route vs. the add-on on the badge example."""
    svg = os.path.join(EXAMPLES, "badge.svg")
    panels = []
    clear_scene()
    bpy.ops.import_curve.svg(filepath=svg)
    curves = [o for o in bpy.data.objects if o.type == "CURVE"]
    for o in curves:
        o.select_set(True)
    bpy.context.view_layer.objects.active = curves[0]
    bpy.ops.object.convert(target="MESH")
    panels.append(("Blender: SVG import + Convert to Mesh", capture(mesh_objects())))
    for topo, label in (("NGON", "Add-on: Clean N-Gons"), ("QUADS", "Add-on: Quads")):
        clear_scene()
        panels.append((label, capture(import_svg(svg, topology=topo, extrude=False, separate="COLOR"))))
    fig, axes = plt.subplots(1, 3, figsize=(16, 6.2))
    for ax, (label, snap) in zip(axes, panels):
        v, f, _t = snap["stats"]
        draw_objs(ax, snap, "%s\n%d vertices, %d faces" % (label, v, f), colored=False)
    save(fig, "compare_svg.png")


def fig_topologies():
    """The four topology modes on the same multi-colored logo."""
    svg = os.path.join(EXAMPLES, "mountain_logo.svg")
    modes = [("NGON", "Clean N-Gons"), ("TRIS", "Triangles"), ("UNIFORM", "Uniform Triangles"), ("QUADS", "Quads")]
    fig, axes = plt.subplots(1, 4, figsize=(18, 5.2))
    for ax, (topo, label) in zip(axes, modes):
        clear_scene()
        snap = capture(import_svg(svg, topology=topo, extrude=False, separate="COLOR", grid_size=2.5))
        v, f, _t = snap["stats"]
        draw_objs(ax, snap, "%s\n%d vertices, %d faces" % (label, v, f), lw=0.3)
    save(fig, "topologies.png")


def render_text_image(text, size_px, fontsize):
    fig = plt.figure(figsize=(size_px / 100, size_px / 100), dpi=100)
    canvas = FigureCanvasAgg(fig)
    fig.text(0.5, 0.45, text, fontsize=fontsize, fontweight="bold", ha="center", va="center", color="black")
    canvas.draw()
    img = np.asarray(canvas.buffer_rgba(), dtype=np.float32) / 255.0
    plt.close(fig)
    return img


def fig_trace_steps():
    """Pixels -> sub-pixel contour -> Bézier curves -> mesh."""
    img = render_text_image("R", 56, 40)
    rgba = np.flipud(img)  # tracer expects the bottom row first
    h, w = rgba.shape[:2]
    settings = TraceSettings()
    shapes, _w, _h = trace_image(rgba, settings)

    from svg_to_mesh.core.tracer import build_layers

    field, iso, _c, _n = build_layers(rgba, settings)[0]
    contours = marching_squares(field, iso)

    clear_scene()
    st = pipeline.ImportSettings(scale_mode="KEEP", origin="KEEP", depth=0.0, create_materials=False)
    objs = pipeline.build_objects(bpy.context, shapes, st, "trace")

    fig, axes = plt.subplots(1, 4, figsize=(18, 5.0))
    extent = (0, w, 0, h)
    gray = rgba[..., :3].mean(axis=2)
    for ax in axes:
        ax.set_xlim(0, w)
        ax.set_ylim(0, h)
        ax.set_aspect("equal")
        ax.axis("off")
    axes[0].imshow(gray, cmap="gray", origin="lower", extent=extent, interpolation="nearest")
    axes[0].set_title("1. Input: %d x %d px image" % (w, h))

    axes[1].imshow(gray, cmap="gray", origin="lower", extent=extent, interpolation="nearest", alpha=0.35)
    axes[1].add_collection(LineCollection([np.vstack([c, c[:1]]) for c in contours], colors=RED, linewidths=1.2))
    axes[1].set_title("2. Sub-pixel contour (%d points)" % sum(len(c) for c in contours))

    n_seg = 0
    for sp in shapes[0].subpaths:
        pts = np.array(flatten_subpath(sp, 0.02))
        axes[2].plot(pts[:, 0], pts[:, 1], color=NAVY, lw=1.6)
        for p0, c1, c2, p3 in sp.segments:
            n_seg += 1
            line = abs((c1[0] - p0[0]) * (p3[1] - p0[1]) - (c1[1] - p0[1]) * (p3[0] - p0[0])) < 1e-6
            if not line:
                axes[2].plot([p0[0], c1[0]], [p0[1], c1[1]], color=RED, lw=0.8)
                axes[2].plot([p3[0], c2[0]], [p3[1], c2[1]], color=RED, lw=0.8)
                axes[2].plot([c1[0], c2[0]], [c1[1], c2[1]], "o", color=RED, ms=2.5)
            axes[2].plot([p0[0]], [p0[1]], "s", color=NAVY, ms=4)
    axes[2].set_title("3. Fitted Bezier curves (%d segments)" % n_seg)

    v, f, _t = mesh_stats(objs)
    for o in objs:
        axes[3].add_collection(PolyCollection(top_polys(o), facecolors=FILL, edgecolors=NAVY, linewidths=0.6))
    axes[3].set_title("4. Clean mesh (%d vertices, %d faces)" % (v, f))
    save(fig, "trace_steps.png")


def fig_trace_demo():
    """A multi-colored logo image traced into one mesh per color."""
    from matplotlib.patches import Circle, FancyBboxPatch

    fig = plt.figure(figsize=(6, 2.4), dpi=100)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 600)
    ax.set_ylim(0, 240)
    ax.axis("off")
    ax.add_patch(Circle((120, 120), 95, color=RED))
    ax.add_patch(Circle((120, 120), 55, color="white"))
    ax.add_patch(FancyBboxPatch((95, 70), 50, 100, boxstyle="round,pad=0,rounding_size=12", color=NAVY))
    ax.text(240, 120, "LOGO", fontsize=72, fontweight="bold", va="center", color=NAVY)
    canvas.draw()
    img = np.asarray(canvas.buffer_rgba(), dtype=np.float32) / 255.0
    plt.close(fig)
    os.makedirs(TMP, exist_ok=True)
    png = os.path.join(TMP, "logo.png")
    plt.imsave(png, img)

    clear_scene()
    bpy.ops.import_mesh.image_trace(filepath=png, trace_mode="COLORS", num_colors=4, separate="COLOR", extrude=False)
    snap = capture(mesh_objects())
    v, _f, _t = snap["stats"]
    fig, axes = plt.subplots(1, 2, figsize=(15, 3.8))
    axes[0].imshow(img)
    axes[0].axis("off")
    axes[0].set_title("Input: PNG (600 x 240 px)")
    draw_objs(axes[1], snap, "Result: one mesh per color, clean n-gons (%d vertices)" % v, lw=0.4)
    save(fig, "trace_demo.png")


def fig_text():
    """Blender text object: built-in conversion vs. the add-on."""
    panels = []
    for mode in ("BLENDER", "NGON", "QUADS"):
        clear_scene()
        bpy.ops.object.text_add()
        txt = bpy.context.active_object
        txt.data.body = "Mesh 42"
        if mode == "BLENDER":
            bpy.ops.object.convert(target="MESH")
            objs = mesh_objects()
            label = "Blender: Convert to Mesh"
        else:
            bpy.ops.object.svgmesh_curves_to_mesh(topology=mode, extrude=False, grid_size=1.2)
            objs = mesh_objects()
            label = "Add-on: " + ("Clean N-Gons" if mode == "NGON" else "Quads")
        panels.append((label, capture(objs)))
    fig, axes = plt.subplots(3, 1, figsize=(11, 9.5))
    for ax, (label, snap) in zip(axes, panels):
        v, f, _t = snap["stats"]
        draw_objs(ax, snap, "%s: %d vertices, %d faces" % (label, v, f), colored=False, lw=0.4)
    save(fig, "text_to_mesh.png")


def fig_strokes():
    """Line icons: strokes are turned into real outlines."""
    svg = os.path.join(EXAMPLES, "line_icon.svg")
    clear_scene()
    bpy.ops.import_curve.svg(filepath=svg)
    curves = [o for o in bpy.data.objects if o.type == "CURVE"]
    for o in curves:
        o.select_set(True)
    bpy.context.view_layer.objects.active = curves[0]
    bpy.ops.object.convert(target="MESH")
    native = capture(mesh_objects())
    clear_scene()
    ours = capture(import_svg(svg, topology="NGON", extrude=False))
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.4))
    v, f, _t = native["stats"]
    draw_objs(axes[0], native, "Blender: SVG import + Convert to Mesh\n(%d faces: only thin wire edges, no surface)" % f,
              colored=False, lw=0.5)
    v, f, _t = ours["stats"]
    draw_objs(axes[1], ours, "Add-on: strokes become outlines\n(%d vertices, %d faces)" % (v, f), lw=0.5)
    for ax in axes:
        ax.margins(0.08)
    save(fig, "strokes.png")


# --------------------------------------------------------------------------
# 3D renders (Cycles)
# --------------------------------------------------------------------------


PREVIEW = bool(os.environ.get("PREVIEW"))  # quick low-quality renders while iterating


def setup_render(width=1200, height=700, samples=128):
    if PREVIEW:
        width, height, samples = width // 2, height // 2, 16
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    scene.cycles.max_bounces = 6
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    scene.world = world
    if world.node_tree is None:
        world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    bg.inputs["Color"].default_value = (0.78, 0.82, 0.88, 1.0)
    bg.inputs["Strength"].default_value = 0.6


def principled(name, hex_color, roughness=0.5):
    mat = bpy.data.materials.new(name)
    rgb = tuple(int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    lin = tuple(pipeline.srgb_to_linear(c) for c in rgb)
    mat.diffuse_color = (*lin, 1.0)
    if mat.node_tree is None:
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*lin, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return mat


def add_floor(hex_color="#e7ebf0"):
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0))
    floor = bpy.context.active_object
    floor.data.materials.append(principled("Floor", hex_color, 0.85))
    return floor


def add_lights():
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
    sun.data.energy = 2.6
    sun.data.angle = math.radians(3)
    sun.rotation_euler = (math.radians(52), math.radians(-12), math.radians(-38))
    bpy.context.scene.collection.objects.link(sun)
    area = bpy.data.objects.new("Fill", bpy.data.lights.new("Fill", "AREA"))
    area.data.energy = 120
    area.data.size = 4
    area.location = (-2.5, -2.5, 3)
    area.rotation_euler = (math.radians(50), 0, math.radians(-45))
    bpy.context.scene.collection.objects.link(area)


def add_camera(location, target, lens=50):
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    cam.data.lens = lens
    cam.location = location
    bpy.context.scene.collection.objects.link(cam)
    empty = bpy.data.objects.new("Target", None)
    empty.location = target
    bpy.context.scene.collection.objects.link(empty)
    con = cam.constraints.new("TRACK_TO")
    con.target = empty
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    bpy.context.scene.camera = cam
    return cam


def add_bevel(obj, width):
    mod = obj.modifiers.new("Bevel", "BEVEL")
    mod.width = width
    mod.segments = 2
    mod.limit_method = "ANGLE"
    return mod


def render(name):
    path = os.path.join(DOCS, name)
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("wrote", path)


LOGO = os.path.join(DOCS, "logo.svg")
LOGO_HEIGHTS = {"#e63946": 1.9}  # the red curve stands out, the cube faces get height 1.0


def import_logo(depth=0.04):
    objs = import_svg(LOGO, separate="COLOR", depth=depth, target_size=1.0, origin="CENTER")
    for o in objs:
        mat = o.data.materials[0]
        mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.35
    return objs


def logo_heights(objs, t, base_depth):
    """Heights of the logo objects at animation time t (0 = flat, 1 = final)."""
    from svg_to_mesh import depth_tools

    ease = t * t * (3.0 - 2.0 * t)
    heights = {}
    for o in objs:
        final = LOGO_HEIGHTS.get(o.get("svgmesh_color"), 1.0)
        heights[o] = (0.04 + ease * (final - 0.04), 0.0, "")
    depth_tools.apply_heights(objs, heights, base_depth)


def logo_scene(width, height, samples, floor="#e9edf2", camera=(0.0, -1.82, 1.72)):
    clear_scene()
    setup_render(width, height, samples)
    objs = import_logo()
    pivot = bpy.data.objects.new("Pivot", None)
    bpy.context.scene.collection.objects.link(pivot)
    for o in objs:
        o.parent = pivot
        add_bevel(o, 0.0025)
    if floor:
        add_floor(floor)
    add_lights()
    add_camera(camera, (0.0, 0.02, 0.03), lens=50)
    return objs, pivot


def render_logo_hero():
    """Animated title image: the flat logo rises into a 3D relief (docs/hero.gif)."""
    from PIL import Image

    frames = 4 if PREVIEW else 24
    objs, pivot = logo_scene(800, 450, 48)
    os.makedirs(TMP, exist_ok=True)
    paths = []
    for k in range(frames):
        t = k / (frames - 1)
        logo_heights(objs, t, 0.04)
        pivot.rotation_euler.z = math.radians(-28.0 + 28.0 * t)
        path = os.path.join(TMP, "hero_%02d.png" % k)
        bpy.context.scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        paths.append(path)
    images = [Image.open(p).convert("RGB") for p in paths]
    # one shared palette keeps the loop free of flicker
    palette = images[-1].quantize(colors=192, method=Image.Quantize.MEDIANCUT)
    seq = images + [images[-1]] * 14 + images[::-1] + [images[0]] * 8
    quantized = [im.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for im in seq]
    out = os.path.join(DOCS, "hero.gif")
    quantized[0].save(out, save_all=True, append_images=quantized[1:], duration=55, loop=0, optimize=True)
    print("wrote", out, "%.1f MB" % (os.path.getsize(out) / 1e6))

    # high quality still of the final state (social preview, fallback)
    objs, pivot = logo_scene(1000, 1000, 160, floor=None, camera=(0.0, -1.5, 1.42))
    logo_heights(objs, 1.0, 0.04)
    bpy.context.scene.render.film_transparent = True
    render("logo_3d.png")


def render_terrace():
    """Multi-colored artwork, one object per color, terraced by paint order."""
    clear_scene()
    setup_render(1200, 680, 128)
    objs = import_svg(os.path.join(EXAMPLES, "mountain_logo.svg"), separate="COLOR", depth=0.03,
                      ignore_white=False, target_size=1.0, origin="CENTER")
    # every color a bit higher than the one below it ("Terrace by Order" in the sidebar)
    bpy.ops.object.svgmesh_terrace(base_depth=0.03, step=0.7)
    for o in objs:
        add_bevel(o, 0.002)
        mat = o.data.materials[0]
        mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.4
    add_floor()
    add_lights()
    add_camera((0.0, -1.55, 1.75), (0.0, 0.03, 0.02), lens=50)
    render("terrace.png")


def fig_logo_png():
    """Flat PNG versions of the logo (docs + Blender panel icon)."""
    clear_scene()
    snap = capture(import_svg(LOGO, separate="COLOR", extrude=False))
    for path, px in ((os.path.join(DOCS, "logo.png"), 512),
                     (os.path.join(ROOT, "svg_to_mesh", "icons", "logo.png"), 64)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fig = plt.figure(figsize=(px / 100, px / 100), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        for polys, color in snap["parts"]:
            ax.add_collection(PolyCollection(polys, facecolors=[color], edgecolors=[color], linewidths=0.2))
        ax.autoscale()
        ax.set_aspect("equal")
        ax.margins(0.03)
        ax.axis("off")
        fig.savefig(path, dpi=100, transparent=True)
        plt.close(fig)
        print("wrote", path)


def fig_social_preview():
    """1280 x 640 image GitHub shows when the repository link is shared."""
    import matplotlib.image as mpimg

    logo = mpimg.imread(os.path.join(DOCS, "logo_3d.png"))
    fig = plt.figure(figsize=(12.8, 6.4), dpi=100)
    fig.patch.set_facecolor("#f1f4f8")
    ax = fig.add_axes([0.03, 0.05, 0.42, 0.9])
    ax.imshow(logo)
    ax.axis("off")
    fig.text(0.48, 0.66, "SVG to Clean Mesh", fontsize=44, fontweight="bold", color=NAVY)
    fig.text(0.485, 0.54, "Free Blender add-on", fontsize=24, color="#457b9d")
    lines = ["SVG files and logo images to clean,", "manifold meshes - ready for booleans,",
             "engraving and 3D printing."]
    for i, line in enumerate(lines):
        fig.text(0.485, 0.41 - i * 0.075, line, fontsize=21, color="#33415c")
    fig.text(0.485, 0.12, "Blender 3.6 - 5.0  |  GPL-3.0", fontsize=16, color="#6b7a90")
    path = os.path.join(DOCS, "social_preview.png")
    fig.savefig(path, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    print("wrote", path)


def render_boolean():
    """The same logo engraved into one block and embossed onto another."""
    clear_scene()
    setup_render(1200, 600, 128)
    stone = principled("Stone", "#c9b79c", 0.55)
    blocks = []
    for x, op in ((-0.62, "DIFFERENCE"), (0.62, "UNION")):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, 0, 0.15))
        block = bpy.context.active_object
        block.scale = (1.0, 1.0, 0.3)
        bpy.ops.object.transform_apply(scale=True)
        block.data.materials.append(stone)
        logo = import_svg(LOGO, separate="ONE", target_size=0.82,
                          depth=0.08 if op == "DIFFERENCE" else 0.05, center_depth=op == "DIFFERENCE",
                          create_materials=False)[0]
        logo.location = (x, 0, 0.3 if op == "DIFFERENCE" else 0.299)
        for o in list(bpy.context.selected_objects):
            o.select_set(False)
        logo.select_set(True)
        block.select_set(True)
        bpy.context.view_layer.objects.active = block
        bpy.ops.object.svgmesh_boolean(operation=op, apply=True)
        add_bevel(block, 0.004)
        blocks.append(block)
    add_floor("#c9d2dd")
    add_lights()
    add_camera((0.0, -2.75, 2.45), (0.0, 0.0, 0.1), lens=50)
    render("boolean.png")


FIGURES = {
    "compare": fig_compare_svg,
    "topologies": fig_topologies,
    "trace_steps": fig_trace_steps,
    "trace_demo": fig_trace_demo,
    "text": fig_text,
    "strokes": fig_strokes,
    "logo_png": fig_logo_png,
    "hero": render_logo_hero,
    "terrace": render_terrace,
    "boolean": render_boolean,
    "social": fig_social_preview,
}


def main():
    svg_to_mesh.register()
    os.makedirs(DOCS, exist_ok=True)
    names = sys.argv[1:] or list(FIGURES)
    for name in names:
        FIGURES[name]()


if __name__ == "__main__":
    main()
