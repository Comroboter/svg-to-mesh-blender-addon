"""Integration tests that need Blender's Python modules.

Run with the ``bpy`` wheel from PyPI (``pip install bpy``) or inside Blender.
They are skipped automatically when ``bpy`` is not importable.
"""

import os

import pytest

bpy = pytest.importorskip("bpy")
import bmesh  # noqa: E402

import svg_to_mesh  # noqa: E402
from svg_to_mesh import mesh_builder, pipeline  # noqa: E402
from svg_to_mesh.core.svg_parser import parse_svg  # noqa: E402

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "badge.svg")

TEST_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">
  <rect width="100" height="100" fill="#fff"/>
  <rect x="10" y="10" width="80" height="80" fill="#ff0000"/>
  <rect x="30" y="30" width="40" height="40" fill="#ffffff"/>
  <rect x="45" y="0" width="10" height="100" fill="#0000ff"/>
</svg>"""


@pytest.fixture(scope="module", autouse=True)
def addon():
    svg_to_mesh.register()
    yield
    svg_to_mesh.unregister()


@pytest.fixture(autouse=True)
def empty_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    yield


def check_solid(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        assert all(e.is_manifold for e in bm.edges), "non-manifold edges"
        vol = bm.calc_volume(signed=True)
        assert vol > 0, "normals point inwards"
        return vol, len(bm.verts), len(bm.faces)
    finally:
        bm.free()


def build(topology, separate="ONE", depth=0.1, overlap="VISIBLE"):
    doc = parse_svg(TEST_SVG)
    st = pipeline.ImportSettings(
        mesh=mesh_builder.MeshSettings(topology=topology, overlap=overlap),
        separate=separate, ignore_white=True, depth=depth, scale_mode="REAL", unit_scale=0.01,
    )
    return pipeline.build_objects(bpy.context, doc.shapes, st, "T")


@pytest.mark.parametrize("topology", ["NGON", "TRIS", "UNIFORM", "QUADS"])
def test_visible_union_with_hole(topology):
    objs = build(topology)
    assert len(objs) == 1
    vol, _v, _f = check_solid(objs[0])
    # red 0.8^2 - white hole 0.4^2 + blue bar parts outside (2 * 0.1 * 0.1) + inside hole (0.1 * 0.4)
    area = 0.64 - 0.16 + 0.02 + 0.04
    assert vol == pytest.approx(area * 0.1, rel=1e-4)


def test_ngon_is_minimal():
    objs = build("NGON", depth=0.0)
    me = objs[0].data
    assert len(me.vertices) <= 24


def test_per_color_objects_share_borders():
    objs = build("NGON", separate="COLOR")
    names = sorted(o.name for o in objs)
    assert names == ["T #0000ff", "T #ff0000"]
    vols = {o.name: check_solid(o)[0] for o in objs}
    assert vols["T #0000ff"] == pytest.approx(0.1 * 1.0 * 0.1, rel=1e-4)
    assert vols["T #ff0000"] == pytest.approx((0.64 - 0.16 - 0.04) * 0.1, rel=1e-4)


def test_union_mode_keeps_shapes_whole():
    objs = build("NGON", separate="COLOR", overlap="UNION")
    vols = {o.name: check_solid(o)[0] for o in objs}
    assert vols["T #ff0000"] == pytest.approx(0.64 * 0.1, rel=1e-4)


def test_import_operator_and_boolean():
    res = bpy.ops.import_mesh.svg_clean(filepath=os.path.abspath(EXAMPLE), depth=0.05, center_depth=True)
    assert res == {"FINISHED"}
    logo = bpy.context.active_object
    check_solid(logo)
    assert logo.dimensions.x == pytest.approx(1.0, rel=1e-3)
    bpy.ops.mesh.primitive_cube_add(size=1.5)
    cube = bpy.context.active_object
    logo.location.z = 0.75
    logo.select_set(True)
    bpy.context.view_layer.objects.active = cube
    assert bpy.ops.object.svgmesh_boolean(operation="DIFFERENCE", apply=True) == {"FINISHED"}
    vol, _v, _f = check_solid(cube)
    assert vol < 1.5 ** 3


def test_text_to_mesh():
    bpy.ops.object.text_add()
    bpy.context.active_object.data.body = "Hi!"
    assert bpy.ops.object.svgmesh_curves_to_mesh(depth=0.1) == {"FINISHED"}
    check_solid(bpy.context.active_object)


def test_trace_operator(tmp_path):
    import numpy as np

    h, w = 120, 200
    yy, xx = np.mgrid[0:h, 0:w]
    px = np.ones((h, w, 4), dtype=np.float32)
    px[np.hypot(xx - 60, yy - 60) < 45, :3] = (0.8, 0.1, 0.1)
    px[(abs(xx - 150) < 30) & (abs(yy - 60) < 40), :3] = (0.1, 0.1, 0.8)
    img = bpy.data.images.new("t", w, h, alpha=True)
    img.pixels.foreach_set(px.ravel())
    path = str(tmp_path / "logo.png")
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    res = bpy.ops.import_mesh.image_trace(filepath=path, trace_mode="COLORS", separate="COLOR", save_svg=True)
    assert res == {"FINISHED"}
    objs = list(bpy.context.selected_objects)
    assert len(objs) == 2
    for o in objs:
        check_solid(o)
    assert os.path.exists(str(tmp_path / "logo_traced.svg"))
