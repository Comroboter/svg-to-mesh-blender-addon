"""Integration tests that need Blender's Python modules.

Run with the ``bpy`` wheel from PyPI (``pip install bpy``) or inside Blender.
They are skipped automatically when ``bpy`` is not importable.
"""

import json
import os
import time

import pytest

bpy = pytest.importorskip("bpy")
import bmesh  # noqa: E402

import svg_to_mesh  # noqa: E402
from svg_to_mesh import depth_ops, mesh_builder, pipeline  # noqa: E402
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
        separate=separate, white="HOLE", depth=depth, scale_mode="REAL", unit_scale=0.01,
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


def test_clip_paths_effects_and_transparency():
    from tests.test_core import ILLUSTRATION_SVG

    doc = parse_svg(ILLUSTRATION_SVG)
    keep = {"clipped", "softedge", "shadow", "sheen"}
    doc.shapes = [s for s in doc.shapes if s.name in keep]
    st = pipeline.ImportSettings(separate="SHAPE", depth=0.1, scale_mode="KEEP", origin="KEEP")
    objs = pipeline.build_objects(bpy.context, doc.shapes, st, "I")
    vols = {o.name: check_solid(o)[0] for o in objs}
    # the shadow (blur 40 on a 200 unit shape) and the 30 % sheen are left out,
    # the soft-edged rect (blur 1) stays and hides the clipped rect below it
    assert list(vols) == ["I softedge"]
    st.mesh.overlap = "UNION"
    objs = pipeline.build_objects(bpy.context, doc.shapes, st, "U")
    vols = {o.name: check_solid(o)[0] for o in objs}
    assert vols["U clipped"] == pytest.approx(50 * 100 * 0.1, rel=1e-4)  # left half only
    assert vols["U softedge"] == pytest.approx(100 * 100 * 0.1, rel=1e-4)
    st.skip_effects, st.min_opacity = False, 0.0
    objs = pipeline.build_objects(bpy.context, doc.shapes, st, "A")
    assert len(objs) == 4


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


# --------------------------------------------------------------------------
# Depth per object (terrace, AI suggestions with a mocked API)
# --------------------------------------------------------------------------


def import_mountain(depth=0.03):
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "mountain_logo.svg")
    res = bpy.ops.import_mesh.svg_clean(filepath=os.path.abspath(path), separate="COLOR", depth=depth,
                                        white="KEEP")
    assert res == {"FINISHED"}
    return list(bpy.context.selected_objects)


def top_z(obj):
    return max((obj.matrix_world @ v.co).z for v in obj.data.vertices)


def test_terrace_by_order():
    objs = import_mountain()
    assert bpy.ops.object.svgmesh_terrace(base_depth=0.01, step=1.0) == {"FINISHED"}
    ordered = sorted(objs, key=lambda o: o["svgmesh_layer"])
    tops = [top_z(o) for o in ordered]
    assert tops == pytest.approx([0.01 * (1 + i) for i in range(len(ordered))])
    for o in objs:
        check_solid(o)


def test_ai_depth_with_mocked_api(monkeypatch):
    from svg_to_mesh.core import ai_client

    objs = import_mountain(depth=0.0)  # flat objects get solidified
    sent = {}

    def fake_call(request, key="", **kw):
        sent["key"], sent["body"] = key, request.body
        text = request.body["messages"][0]["content"][-1]["text"]
        regions = json.loads(text[text.index("["):text.rindex("]") + 1])
        sent["regions"] = regions
        out = [{"id": r["id"], "height": 1.0 + r["layer"] * 0.5, "base": 0.0, "reason": "layer %d" % r["layer"]}
               for r in regions]
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(
            {"regions": out, "summary": "terraced"})}]}

    monkeypatch.setattr(ai_client, "call_api", fake_call)
    monkeypatch.setattr(depth_ops, "online_allowed", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for o in objs:
        o.select_set(True)
    assert bpy.ops.object.svgmesh_ai_depth(base_depth=0.02, hint="wall sign") == {"FINISHED"}
    assert sent["key"] == "test-key"
    assert len(sent["regions"]) == len(objs)
    assert {r["color"] for r in sent["regions"]} == {o["svgmesh_color"] for o in objs}
    content = sent["body"]["messages"][0]["content"]
    assert "wall sign" in content[-1]["text"]
    assert [c["type"] for c in content] == ["image", "image", "text"]  # preview + region map
    assert len({r["map_color"] for r in sent["regions"]}) == len(objs)
    for o in objs:
        vol = check_solid(o)[0]
        assert vol > 0
        assert top_z(o) == pytest.approx(0.02 * (1.0 + o["svgmesh_layer"] * 0.5))
        assert o["svgmesh_reason"] == "layer %d" % o["svgmesh_layer"]
    # re-applying with another base depth keeps the proportions
    assert bpy.ops.object.svgmesh_reapply_depth(base_depth=0.04) == {"FINISHED"}
    for o in objs:
        assert top_z(o) == pytest.approx(0.04 * (1.0 + o["svgmesh_layer"] * 0.5))


def test_ai_depth_needs_key(monkeypatch):
    objs = import_mountain()
    monkeypatch.setattr(depth_ops, "online_allowed", lambda: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for o in objs:
        o.select_set(True)
    with pytest.raises(RuntimeError, match="not set up"):
        bpy.ops.object.svgmesh_ai_depth()


def test_ai_depth_respects_offline_mode():
    objs = import_mountain()
    for o in objs:
        o.select_set(True)
    if getattr(bpy.app, "online_access", True):
        pytest.skip("online access is enabled in this Blender")
    with pytest.raises(RuntimeError, match="Online access is disabled"):
        bpy.ops.object.svgmesh_ai_depth()


def _fake_ai(monkeypatch, step=0.5):
    from svg_to_mesh.core import ai_client

    def fake_call(request, key="", **kw):
        text = request.body["messages"][0]["content"][-1]["text"]
        regions = json.loads(text[text.index("["):text.rindex("]") + 1])
        out = [{"id": r["id"], "height": 1.0 + r["layer"] * step, "base": 0.0, "reason": "r"} for r in regions]
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(
            {"regions": out, "summary": "ok"})}]}

    monkeypatch.setattr(ai_client, "call_api", fake_call)
    monkeypatch.setattr(depth_ops, "online_allowed", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_auto_base_depth_follows_object_scale(monkeypatch):
    """Scaled objects: 'Base Depth = 0' uses their current thickness, not a fixed value."""
    _fake_ai(monkeypatch)
    objs = import_mountain(depth=0.03)
    for o in objs:
        o.scale = (10.0, 10.0, 10.0)  # the user scales the logo up after importing
        o.select_set(True)
    bpy.context.view_layer.update()
    assert bpy.context.scene.svgmesh_depth_unit == 0.0  # automatic by default
    assert bpy.ops.object.svgmesh_ai_depth() == {"FINISHED"}  # base_depth 0 = auto
    for o in objs:
        check_solid(o)
        assert top_z(o) == pytest.approx(0.3 * (1.0 + 0.5 * o["svgmesh_layer"]), rel=1e-4)
    # applying again keeps the same scale (no creeping)
    assert bpy.ops.object.svgmesh_ai_depth() == {"FINISHED"}
    for o in objs:
        assert top_z(o) == pytest.approx(0.3 * (1.0 + 0.5 * o["svgmesh_layer"]), rel=1e-4)


def test_auto_base_depth_for_flat_objects(monkeypatch):
    _fake_ai(monkeypatch, step=0.0)
    objs = import_mountain(depth=0.0)
    for o in objs:
        o.select_set(True)
    assert bpy.ops.object.svgmesh_terrace(step=0.0) == {"FINISHED"}
    for o in objs:
        check_solid(o)
        assert top_z(o) == pytest.approx(0.05, rel=1e-3)  # 5 % of the 1 m wide logo


def test_tiny_base_depth_warns(monkeypatch):
    from svg_to_mesh import depth_tools

    objs = import_mountain(depth=0.03)
    value, warning = depth_tools.resolve_base_depth(objs, 0.00001)
    assert value == 0.00001 and "very thin" in warning
    assert depth_tools.resolve_base_depth(objs, 0.0)[1] is None


def test_ai_progress_status():
    depth_ops.AI_JOB.clear()
    assert depth_ops.ai_job_status() is None
    depth_ops.AI_JOB["start"] = 100.0
    elapsed, factor = depth_ops.ai_job_status(now=110.0)
    assert elapsed == pytest.approx(10.0) and 0.0 < factor < 0.95
    assert depth_ops.ai_job_status(now=400.0)[1] == pytest.approx(0.95)
    assert depth_ops.ai_job_status(now=100.0 + depth_ops.AI_STALE_SECONDS + 1) is None  # stale job is dropped
    assert not depth_ops.AI_JOB
    depth_ops.AI_JOB["start"] = time.time()
    try:
        assert bpy.ops.object.svgmesh_ai_cancel() == {"FINISHED"}
        assert depth_ops.AI_JOB["cancel"]
    finally:
        depth_ops.AI_JOB.clear()


def test_update_operators_are_registered():
    from svg_to_mesh import update_ops

    import build

    assert update_ops.current_version() == tuple(int(x) for x in build.get_version().split("."))
    assert hasattr(bpy.ops.preferences, "svgmesh_check_update")
    assert not bpy.ops.preferences.svgmesh_install_update.poll()  # nothing checked yet


def test_split_parts_gives_each_part_its_own_object():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">
      <rect x="0" y="0" width="30" height="30"/><rect x="40" y="0" width="30" height="30"/>
      <rect x="80" y="0" width="0.5" height="0.5"/><rect x="90" y="0" width="0.5" height="0.5"/></svg>"""
    doc = parse_svg(svg)
    st = pipeline.ImportSettings(separate="AUTO", depth=0.1, scale_mode="KEEP", origin="KEEP")
    objs = pipeline.build_objects(bpy.context, doc.shapes, st, "P")
    assert len(objs) == 1  # one color: AUTO makes a single object
    obj = objs[0]
    obj["svgmesh_height"] = 2.0
    pipeline.select_objects(bpy.context, [obj])
    assert bpy.ops.object.svgmesh_split_parts() == {"FINISHED"}
    parts = sorted(bpy.context.selected_objects, key=lambda o: o.name)
    assert [o.name for o in parts] == ["P details", "P part 1", "P part 2"]
    vols = {o.name: check_solid(o)[0] for o in parts}
    assert vols["P part 1"] == pytest.approx(900 * 0.1, rel=1e-4)
    assert vols["P details"] == pytest.approx(2 * 0.25 * 0.1, rel=1e-4)  # tiny parts stay together
    assert all(o["svgmesh_height"] == 2.0 for o in parts)  # properties are kept
    assert "P" not in bpy.data.objects


def test_auto_objects_split_colorful_artwork():
    st = pipeline.ImportSettings(separate="AUTO", white="AUTO", depth=0.1, scale_mode="REAL", unit_scale=0.01)
    objs = pipeline.build_objects(bpy.context, parse_svg(TEST_SVG).shapes, st, "A")
    # white background removed (touches the outside), the enclosed white square is kept as its own color
    assert sorted(o.name for o in objs) == ["A #0000ff", "A #ff0000", "A #ffffff"]
    vols = {o.name: check_solid(o)[0] for o in objs}
    assert vols["A #ffffff"] == pytest.approx((0.16 - 0.04) * 0.1, rel=1e-4)


def test_white_hole_background_through_letter_holes():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <rect width="100" height="100" fill="#fff"/>
      <path d="M20 20h60v60h-60z M40 40v20h20v-20z" fill="#000" fill-rule="evenodd"/></svg>"""
    st = pipeline.ImportSettings(white="AUTO", depth=0.1, scale_mode="REAL", unit_scale=0.01)
    objs = pipeline.build_objects(bpy.context, parse_svg(svg).shapes, st, "O")
    assert len(objs) == 1
    # the background shows through the hole of the "O": it stays a hole
    assert check_solid(objs[0])[0] == pytest.approx((0.36 - 0.04) * 0.1, rel=1e-4)
