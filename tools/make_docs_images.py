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
# Animated GIF helpers
# --------------------------------------------------------------------------


def fig_to_image(fig):
    from PIL import Image

    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    return Image.fromarray(np.asarray(canvas.buffer_rgba())[..., :3].copy())


def save_gif(frames, durations, name, dither=False, colors=160):
    """Write an endlessly looping GIF with one shared palette (no flicker)."""
    from PIL import Image

    if isinstance(durations, (int, float)):
        durations = [int(durations)] * len(frames)
    sample = frames[:: max(1, len(frames) // 8)]
    w, h = frames[0].size
    montage = Image.new("RGB", (w, h * len(sample)))
    for i, im in enumerate(sample):
        montage.paste(im, (0, i * h))
    palette = montage.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    quantized = [im.quantize(palette=palette, dither=mode) for im in frames]
    path = os.path.join(DOCS, name)
    quantized[0].save(path, save_all=True, append_images=quantized[1:], duration=durations, loop=0)
    print("wrote", path, "%d frames, %.1f MB" % (len(frames), os.path.getsize(path) / 1e6))


def normalize(snap, target, shrink=1.0):
    """Move/scale a snapshot so that its bounding box matches *target* (minx, miny, maxx, maxy).

    *shrink* < 1 maps only that fraction of the box (e.g. to ignore stroke widths).
    """
    pts = [p for polys, _c in snap["parts"] for poly in polys for p in poly] + [p for e in snap["edges"] for p in e]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    sx, sy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    size = max(max(xs) - min(xs), max(ys) - min(ys)) * shrink
    tx, ty = (target[0] + target[2]) / 2, (target[1] + target[3]) / 2
    k = max(target[2] - target[0], target[3] - target[1]) / size

    def f(p):
        return ((p[0] - sx) * k + tx, (p[1] - sy) * k + ty)

    return {"parts": [([[f(p) for p in poly] for poly in polys], c) for polys, c in snap["parts"]],
            "edges": [[f(p) for p in e] for e in snap["edges"]], "stats": snap["stats"]}


def bounds_of(snap):
    pts = [p for polys, _c in snap["parts"] for poly in polys for p in poly] + [p for e in snap["edges"] for p in e]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def draw_snap(ax, snap, colored=True, lw=0.35, edge=NAVY):
    artists = []
    for polys, color in snap["parts"]:
        col = color if colored else FILL
        artists.append(ax.add_collection(PolyCollection(polys, facecolors=[col], edgecolors=edge, linewidths=lw)))
    if snap.get("edges"):
        artists.append(ax.add_collection(LineCollection(snap["edges"], colors=edge, linewidths=1.2)))
    return artists


def slider_gif(name, left, right, left_text, right_text, frames=44, figsize=(9.6, 5.6), left_artists=None,
               xlim=None, ylim=None):
    """Before/after comparison: a divider glides back and forth (smooth, seamless loop).

    left/right: functions(ax) -> list of artists drawn in data coordinates.
    """
    from matplotlib.patches import Rectangle

    fig = plt.figure(figsize=figsize, dpi=100)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.84])
    a_left = left(ax)
    a_right = right(ax)
    ax.set_aspect("equal")
    ax.axis("off")
    if xlim:
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
    else:
        ax.autoscale()
        ax.margins(0.06)
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    big = (y1 - y0) * 10
    line = ax.plot([x0, x0], [y0, y1], color=RED, lw=2.5, solid_capstyle="butt")[0]
    knob = ax.plot([x0], [(y0 + y1) / 2], "o", ms=13, color=RED, mec="white", mew=2.5)[0]
    fig.text(0.03, 0.95, left_text[0], fontsize=13, fontweight="bold", color=NAVY, va="top")
    fig.text(0.03, 0.905, left_text[1], fontsize=10.5, color="#33415c", va="top")
    fig.text(0.97, 0.95, right_text[0], fontsize=13, fontweight="bold", color=NAVY, va="top", ha="right")
    fig.text(0.97, 0.905, right_text[1], fontsize=10.5, color="#33415c", va="top", ha="right")
    images = []
    for k in range(frames):
        t = k / frames
        pos = x0 + (x1 - x0) * (0.5 - 0.42 * math.cos(2 * math.pi * t))
        # matplotlib caches clip paths, so new rectangles are set for every frame
        clip_l = Rectangle((x0 - big, y0 - big), pos - x0 + big, 3 * big, transform=ax.transData)
        clip_r = Rectangle((pos, y0 - big), x1 - pos + big, 3 * big, transform=ax.transData)
        for a in a_left:
            a.set_clip_path(clip_l)
        for a in a_right:
            a.set_clip_path(clip_r)
        line.set_xdata([pos, pos])
        knob.set_xdata([pos])
        images.append(fig_to_image(fig))
    plt.close(fig)
    save_gif(images, 60, name)


def slideshow_gif(name, states, figsize=(9.6, 5.6), hold=1500, limits=None):
    """Cycle through states; each state is (title, subtitle, draw(ax))."""
    images = []
    for title, subtitle, draw in states:
        fig = plt.figure(figsize=figsize, dpi=100)
        ax = fig.add_axes([0.02, 0.02, 0.96, 0.84])
        draw(ax)
        ax.set_aspect("equal")
        ax.axis("off")
        if limits:
            ax.set_xlim(*limits[0])
            ax.set_ylim(*limits[1])
        else:
            ax.autoscale()
            ax.margins(0.04)
        fig.text(0.5, 0.95, title, fontsize=14, fontweight="bold", color=NAVY, va="top", ha="center")
        fig.text(0.5, 0.9, subtitle, fontsize=10.5, color="#33415c", va="top", ha="center")
        images.append(fig_to_image(fig))
        plt.close(fig)
    durations = hold if not isinstance(hold, (list, tuple)) else list(hold)
    save_gif(images, durations, name)


def blender_svg_snapshot(svg):
    """Blender's own route: SVG import as curves, then Convert to Mesh."""
    clear_scene()
    bpy.ops.import_curve.svg(filepath=svg)
    curves = [o for o in bpy.data.objects if o.type == "CURVE"]
    for o in curves:
        o.select_set(True)
    bpy.context.view_layer.objects.active = curves[0]
    bpy.ops.object.convert(target="MESH")
    return capture(mesh_objects())


def problems(snap_objs):
    """(duplicate vertices, broken edges) of the current mesh objects."""
    import bmesh
    from mathutils.kdtree import KDTree

    dup = bad = 0
    for o in snap_objs:
        bm = bmesh.new()
        bm.from_mesh(o.data)
        kd = KDTree(len(bm.verts))
        for i, v in enumerate(bm.verts):
            kd.insert(v.co, i)
        kd.balance()
        dup += sum(1 for v in bm.verts if len(kd.find_range(v.co, 1e-6)) > 1)
        bad += sum(1 for e in bm.edges if not e.link_faces or (not e.is_manifold and not e.is_boundary))
        bm.free()
    return dup, bad


# --------------------------------------------------------------------------
# 2D figures (animated)
# --------------------------------------------------------------------------


def fig_compare_svg():
    """Slider: Blender's built-in route vs. the add-on on the badge example."""
    svg = os.path.join(EXAMPLES, "badge.svg")
    blender = blender_svg_snapshot(svg)
    b_dup, b_bad = problems(mesh_objects())
    clear_scene()
    ours = capture(import_svg(svg, topology="NGON", extrude=False, separate="COLOR"))
    o_dup, o_bad = problems(mesh_objects())
    target = bounds_of(ours)
    blender = normalize(blender, target)
    bv, bf, _ = blender["stats"]
    ov, of, _ = ours["stats"]
    slider_gif(
        "compare_svg.gif",
        lambda ax: draw_snap(ax, blender, colored=False, lw=0.45),
        lambda ax: draw_snap(ax, ours, colored=False, lw=0.6),
        ("Blender: import + Convert to Mesh", "%d faces, %d duplicate vertices, %d broken edges" % (bf, b_dup, b_bad)),
        ("SVG to Clean Mesh", "%d faces, %d duplicate vertices, %d broken edges" % (of, o_dup, o_bad)),
        figsize=(8.4, 6.0),
    )


def fig_topologies():
    """Cycle through the four topology modes on the same multi-colored artwork."""
    svg = os.path.join(EXAMPLES, "mountain_logo.svg")
    modes = [("NGON", "Clean N-Gons", "fewest faces, ideal for booleans"),
             ("TRIS", "Triangles", "constrained Delaunay, outline vertices only"),
             ("UNIFORM", "Uniform Triangles", "even triangles for displacement and deformation"),
             ("QUADS", "Quads", "quad-dominant grid for subdivision and sculpting")]
    states = []
    for topo, label, what in modes:
        clear_scene()
        snap = capture(import_svg(svg, topology=topo, extrude=False, separate="COLOR", grid_size=2.5))
        v, f, _t = snap["stats"]
        states.append((label, "%s  |  %d vertices, %d faces" % (what, v, f),
                       lambda ax, snap=snap: draw_snap(ax, snap, lw=0.35)))
    slideshow_gif("topologies.gif", states, figsize=(8.0, 6.4), hold=1600)


def render_text_image(text, size_px, fontsize):
    fig = plt.figure(figsize=(size_px / 100, size_px / 100), dpi=100)
    canvas = FigureCanvasAgg(fig)
    fig.text(0.5, 0.45, text, fontsize=fontsize, fontweight="bold", ha="center", va="center", color="black")
    canvas.draw()
    img = np.asarray(canvas.buffer_rgba(), dtype=np.float32) / 255.0
    plt.close(fig)
    return img


def fig_trace_steps():
    """Pixels -> sub-pixel contour (drawn progressively) -> Bezier curves -> mesh."""
    img = render_text_image("R", 56, 40)
    rgba = np.flipud(img)  # tracer expects the bottom row first
    h, w = rgba.shape[:2]
    settings = TraceSettings()
    shapes, _w, _h = trace_image(rgba, settings)

    from svg_to_mesh.core.tracer import build_layers

    field, iso, _c, _n = build_layers(rgba, settings)[0]
    contours = [np.vstack([c, c[:1]]) for c in marching_squares(field, iso)]
    n_points = sum(len(c) - 1 for c in contours)

    clear_scene()
    st = pipeline.ImportSettings(scale_mode="KEEP", origin="KEEP", depth=0.0, create_materials=False)
    objs = pipeline.build_objects(bpy.context, shapes, st, "trace")
    mesh = capture(objs)
    v, f, _t = mesh["stats"]
    gray = rgba[..., :3].mean(axis=2)
    extent = (0, w, 0, h)
    n_seg = sum(len(sp.segments) for sp in shapes[0].subpaths)

    def pixels(ax, alpha=1.0):
        ax.imshow(gray, cmap="gray", origin="lower", extent=extent, interpolation="nearest", alpha=alpha, vmin=0, vmax=1)

    def contour_upto(ax, frac):
        pixels(ax, 0.3)
        segs = [c[: max(2, int(len(c) * frac))] for c in contours]
        ax.add_collection(LineCollection(segs, colors=RED, linewidths=2.2))

    def bezier(ax):
        for sp in shapes[0].subpaths:
            pts = np.array(flatten_subpath(sp, 0.02))
            ax.plot(pts[:, 0], pts[:, 1], color=NAVY, lw=2.4)
            for p0, c1, c2, p3 in sp.segments:
                straight = abs((c1[0] - p0[0]) * (p3[1] - p0[1]) - (c1[1] - p0[1]) * (p3[0] - p0[0])) < 1e-6
                if not straight:
                    ax.plot([p0[0], c1[0]], [p0[1], c1[1]], color=RED, lw=1.2)
                    ax.plot([p3[0], c2[0]], [p3[1], c2[1]], color=RED, lw=1.2)
                    ax.plot([c1[0], c2[0]], [c1[1], c2[1]], "o", color=RED, ms=5)
                ax.plot([p0[0]], [p0[1]], "s", color=NAVY, ms=7)

    states = [("1. Input image", "%d x %d pixels" % (w, h), pixels)]
    steps = 10
    durations = [1400]
    for k in range(1, steps + 1):
        states.append(("2. Sub-pixel contour", "%d points" % n_points,
                       lambda ax, fr=k / steps: contour_upto(ax, fr)))
        durations.append(90 if k < steps else 1100)
    states.append(("3. Fitted Bezier curves", "%d segments, corners kept sharp" % n_seg, bezier))
    durations.append(1600)
    states.append(("4. Clean mesh", "%d vertices, %d faces" % (v, f),
                   lambda ax: draw_snap(ax, mesh, colored=False, lw=1.0)))
    durations.append(1800)
    slideshow_gif("trace_steps.gif", states, figsize=(6.4, 6.4), hold=durations, limits=((0, w), (0, h)))


def fig_trace_demo():
    """Slider: a multi-colored logo image and the traced mesh (one object per color)."""
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
    ink = img[..., :3].mean(axis=2) < 0.95
    rows, cols = np.nonzero(ink)
    h, w = ink.shape
    target = (cols.min(), h - rows.max() - 1, cols.max() + 1, h - rows.min())
    snap = normalize(snap, target)

    def left(ax):
        return [ax.imshow(img, extent=(0, w, 0, h), interpolation="nearest")]

    slider_gif("trace_demo.gif", left, lambda ax: draw_snap(ax, snap, lw=0.6),
               ("Input: PNG image", "%d x %d pixels" % (w, h)),
               ("Traced: one mesh per color", "%d vertices, clean n-gons" % v),
               figsize=(10.0, 4.6), xlim=(-10, w + 10), ylim=(-10, h + 10))


def fig_text():
    """Cycle: a Blender text object converted by Blender and by the add-on."""
    states = []
    for mode in ("BLENDER", "NGON", "QUADS"):
        clear_scene()
        bpy.ops.object.text_add()
        bpy.context.active_object.data.body = "Mesh 42"
        if mode == "BLENDER":
            bpy.ops.object.convert(target="MESH")
            title = "Blender: Convert to Mesh"
        else:
            bpy.ops.object.svgmesh_curves_to_mesh(topology=mode, extrude=False, grid_size=1.2)
            title = "SVG to Clean Mesh: " + ("Clean N-Gons" if mode == "NGON" else "Quads")
        snap = capture(mesh_objects())
        v, f, _t = snap["stats"]
        states.append((title, "%d vertices, %d faces" % (v, f),
                       lambda ax, snap=snap: draw_snap(ax, snap, colored=False, lw=0.5)))
    slideshow_gif("text_to_mesh.gif", states, figsize=(10.0, 3.9), hold=1700)


def fig_strokes():
    """Slider: line icon imported by Blender (wire edges only) vs. the add-on (real outlines)."""
    svg = os.path.join(EXAMPLES, "line_icon.svg")
    native = blender_svg_snapshot(svg)
    clear_scene()
    ours = capture(import_svg(svg, topology="NGON", extrude=False))
    # the add-on's outline is wider by the stroke width (1.8 of 16 units of center line)
    native = normalize(native, bounds_of(ours), shrink=17.8 / 16.0)
    v, f, _t = ours["stats"]
    slider_gif("strokes.gif",
               lambda ax: draw_snap(ax, native, colored=False, lw=0.6),
               lambda ax: draw_snap(ax, ours, lw=0.6),
               ("Blender: import + Convert to Mesh", "%d faces: thin wire edges, no surface" % native["stats"][1]),
               ("SVG to Clean Mesh", "strokes become outlines: %d vertices, %d faces" % (v, f)),
               figsize=(8.4, 6.0))


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


def render_frames(prefix, count, update):
    """Render *count* frames; update(t) prepares the scene for t in [0, 1)."""
    from PIL import Image

    os.makedirs(TMP, exist_ok=True)
    images = []
    for k in range(count):
        update(k / count)
        path = os.path.join(TMP, "%s_%02d.png" % (prefix, k))
        bpy.context.scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        images.append(Image.open(path).convert("RGB"))
    return images


def smooth_pulse(t, rise=(0.12, 0.42), fall=(0.78, 1.0)):
    """0 -> 1 -> 0 over one loop with soft starts and stops (seamless)."""
    def ease(x):
        x = min(max(x, 0.0), 1.0)
        return x * x * (3.0 - 2.0 * x)

    up = ease((t - rise[0]) / (rise[1] - rise[0]))
    down = ease((t - fall[0]) / (fall[1] - fall[0]))
    return up * (1.0 - down)


def render_logo_hero():
    """Animated title image: the logo turns once and rises into a 3D relief (seamless loop)."""
    frames = 6 if PREVIEW else 60
    objs, pivot = logo_scene(800, 450, 48)

    def update(t):
        logo_heights(objs, smooth_pulse(t), 0.04)
        pivot.rotation_euler.z = math.radians(-360.0 * t)

    save_gif(render_frames("hero", frames, update), 60, "hero.gif", dither=True, colors=192)

    # high quality still of the final state (social preview, fallback)
    objs, pivot = logo_scene(1000, 1000, 160, floor=None, camera=(0.0, -1.5, 1.42))
    logo_heights(objs, 1.0, 0.04)
    bpy.context.scene.render.film_transparent = True
    render("logo_3d.png")


def render_terrace():
    """Multi-colored artwork, one object per color, rising into terraces (seamless loop)."""
    frames = 6 if PREVIEW else 44
    clear_scene()
    setup_render(760, 430, 48)
    objs = import_svg(os.path.join(EXAMPLES, "mountain_logo.svg"), separate="COLOR", depth=0.03,
                      white="KEEP", target_size=1.0, origin="CENTER")
    pivot = bpy.data.objects.new("Pivot", None)
    bpy.context.scene.collection.objects.link(pivot)
    for o in objs:
        o.parent = pivot
        add_bevel(o, 0.002)
        mat = o.data.materials[0]
        mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.4
    add_floor()
    add_lights()
    add_camera((0.0, -1.55, 1.75), (0.0, 0.03, 0.02), lens=50)
    from svg_to_mesh import depth_tools

    ordered = depth_tools.paint_order(objs)

    def update(t):
        # grows from almost flat into "Terrace by Order" (step 0.7), with a gentle sway
        p = smooth_pulse(t, rise=(0.08, 0.45), fall=(0.75, 1.0))
        heights = {o: (0.15 + p * (0.85 + 0.7 * i), 0.0, "") for i, o in enumerate(ordered)}
        depth_tools.apply_heights(ordered, heights, 0.03)
        pivot.rotation_euler.z = math.radians(10.0 * math.sin(2 * math.pi * t))

    save_gif(render_frames("terrace", frames, update), 70, "terrace.gif", dither=True, colors=192)


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
    fig.text(0.485, 0.12, "Blender 3.6 - 5.2  |  GPL-3.0", fontsize=16, color="#6b7a90")
    path = os.path.join(DOCS, "social_preview.png")
    fig.savefig(path, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    print("wrote", path)


def render_boolean():
    """The add-on logo engraved into one block and embossed onto another, live booleans (seamless loop)."""
    frames = 4 if PREVIEW else 36
    clear_scene()
    setup_render(800, 400, 48)
    stone = principled("Stone", "#c9b79c", 0.55)
    cutters = []
    for x, op in ((-0.62, "DIFFERENCE"), (0.62, "UNION")):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, 0, 0.15))
        block = bpy.context.active_object
        block.scale = (1.0, 1.0, 0.3)
        bpy.ops.object.transform_apply(scale=True)
        block.data.materials.append(stone)
        logo = import_svg(LOGO, separate="ONE", target_size=0.82, depth=0.1, center_depth=True,
                          create_materials=False)[0]
        logo.location = (x, 0, 0.36)
        for o in list(bpy.context.selected_objects):
            o.select_set(False)
        logo.select_set(True)
        block.select_set(True)
        bpy.context.view_layer.objects.active = block
        bpy.ops.object.svgmesh_boolean(operation=op, apply=False, hide_cutters=False)
        logo.hide_render = True
        add_bevel(block, 0.004)
        cutters.append((logo, op))
    add_floor("#c9d2dd")
    add_lights()
    add_camera((0.0, -2.75, 2.45), (0.0, 0.0, 0.1), lens=50)

    def update(t):
        amount = smooth_pulse(t, rise=(0.05, 0.42), fall=(0.68, 0.98))
        for logo, op in cutters:
            if op == "DIFFERENCE":  # cutter (10 cm, centered) sinks into the top face
                logo.location.z = 0.3 + 0.0495 - 0.042 * amount
            else:  # the added logo grows out of the top face
                logo.location.z = 0.3 - 0.0495 + 0.048 * amount

    save_gif(render_frames("boolean", frames, update), 70, "boolean.gif", dither=True, colors=160)


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
