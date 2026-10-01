"""Tests for the pure-Python core (no Blender needed, numpy for the tracer)."""

import math

import pytest

from svg_to_mesh.core.geometry import (
    SubPath,
    VectorShape,
    clean_polygon,
    flatten_subpath,
    shape_to_polys,
    signed_area,
    stroke_polygons,
)
from svg_to_mesh.core.svg_parser import parse_color, parse_path_data, parse_svg, parse_transform
from svg_to_mesh.core.svg_writer import shapes_to_svg


def poly_area(shape_polys):
    return sum(abs(signed_area(c)) for p in shape_polys for c in p.contours)


# --------------------------------------------------------------------------
# SVG parsing
# --------------------------------------------------------------------------


def test_path_commands_relative_and_implicit():
    sps = parse_path_data("m10 10 20 0 0 20 -20 0z M50,50 h10 v10 h-10 Z")
    assert len(sps) == 2
    assert all(sp.closed for sp in sps)
    assert sps[0].segments[0][0] == (10, 10)
    assert sps[0].segments[1][3] == (30, 30)
    assert sps[1].segments[-1][3] == (50, 50)


def test_arc_with_compact_flags():
    # "a25 25 0 1010 0" = rx ry rot large=1 sweep=0 x=10 y=0
    sps = parse_path_data("M0 0a25 25 0 1010 0")
    assert len(sps) == 1
    assert sps[0].segments[-1][3] == pytest.approx((10, 0))


def test_full_circle_from_arcs_is_round():
    sps = parse_path_data("M0 -10 A10 10 0 1 1 0 10 A10 10 0 1 1 0 -10 Z")
    pts = flatten_subpath(sps[0], 0.001)
    radii = [math.hypot(x, y) for x, y in pts]
    assert min(radii) == pytest.approx(10, abs=0.01)
    assert max(radii) == pytest.approx(10, abs=0.01)


def test_quadratic_and_smooth_curves():
    sps = parse_path_data("M0 0 Q 5 10 10 0 T 20 0 C 25 5 30 5 35 0 S 45 -5 50 0")
    assert len(sps[0].segments) == 4
    assert sps[0].segments[-1][3] == (50, 0)


def test_transforms():
    m = parse_transform("translate(10,5) scale(2)")
    assert m == pytest.approx((2, 0, 0, 2, 10, 5))
    m = parse_transform("rotate(90 10 10)")
    # (20, 10) rotated 90deg around (10, 10) -> (10, 20)
    x = m[0] * 20 + m[2] * 10 + m[4]
    y = m[1] * 20 + m[3] * 10 + m[5]
    assert (x, y) == pytest.approx((10, 20))


def test_colors():
    assert parse_color("#f00") == pytest.approx((1, 0, 0))
    assert parse_color("rgb(0, 128, 255)") == pytest.approx((0, 128 / 255, 1))
    assert parse_color("none") is None
    assert parse_color("navy") == pytest.approx((0, 0, 128 / 255))


def test_document_styles_use_and_units():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
             width="100mm" height="50mm" viewBox="0 0 200 100">
      <style>.red { fill: #ff0000 } #blue { fill: blue }</style>
      <defs><rect id="r" width="10" height="10"/></defs>
      <rect class="red" x="0" y="0" width="20" height="20"/>
      <circle id="blue" cx="50" cy="50" r="10"/>
      <g fill="#00ff00" transform="translate(100 0)"><use xlink:href="#r" x="5" y="5"/></g>
      <rect x="0" y="0" width="5" height="5" style="display:none"/>
      <path d="M0 0 L10 10" stroke="black" fill="none"/>
    </svg>"""
    doc = parse_svg(svg)
    assert [s.fill for s in doc.shapes[:3]] == [(1, 0, 0), (0, 0, 1), (0, 1, 0)]
    assert len(doc.shapes) == 4
    assert doc.shapes[3].fill is None and doc.shapes[3].stroke == (0, 0, 0)
    # 100mm wide document = 377.95 px, viewBox 200 -> scale 1.89
    rect = doc.shapes[0]
    xs = [p[0] for seg in rect.subpaths[0].segments for p in seg]
    assert max(xs) - min(xs) == pytest.approx(20 * 100 / 25.4 * 96 / 200)
    # y axis flipped: top of the document has the largest y
    ys = [p[1] for seg in rect.subpaths[0].segments for p in seg]
    assert max(ys) == pytest.approx(doc.height_px)


def test_gradient_uses_first_stop_color():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
      <linearGradient id="g"><stop offset="0" stop-color="#123456"/></linearGradient>
      <rect width="10" height="10" fill="url(#g)"/></svg>"""
    doc = parse_svg(svg)
    assert doc.shapes[0].fill == pytest.approx((0x12 / 255, 0x34 / 255, 0x56 / 255))


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def test_clean_polygon_removes_duplicates_and_collinear():
    pts = [(0, 0), (5, 0), (5, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    assert clean_polygon(pts, 1e-6) == [(0, 0), (10, 0), (10, 10), (0, 10)]


def test_flatten_respects_tolerance():
    sp = parse_path_data("M0 0 C 0 50 100 50 100 0")[0]
    coarse = flatten_subpath(sp, 1.0)
    fine = flatten_subpath(sp, 0.01)
    assert len(coarse) < len(fine)
    assert len(flatten_subpath(parse_path_data("M0 0 L100 0")[0], 0.001)) == 2


def test_open_stroke_area():
    polys = stroke_polygons([(0, 0), (100, 0)], False, 10, cap="butt")
    assert sum(abs(signed_area(p)) for p in polys) == pytest.approx(1000)
    polys = stroke_polygons([(0, 0), (100, 0)], False, 10, cap="square")
    assert sum(abs(signed_area(p)) for p in polys) == pytest.approx(1100)


def winding(polys, pt):
    """Winding number of *pt* with respect to a set of polygons."""
    x, y = pt
    w = 0
    for poly in polys:
        n = len(poly)
        for i in range(n):
            (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
            cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
            if y1 <= y < y2 and cross > 0:
                w += 1
            elif y2 <= y < y1 and cross < 0:
                w -= 1
    return w


def test_closed_stroke_is_ring():
    square = [(0, 0), (100, 0), (100, 100), (0, 100)]
    polys = stroke_polygons(square, True, 10, join="miter")
    assert len(polys) == 2
    # covered with the non-zero rule: the 10 wide frame, not the hole
    for inside in [(-4, 50), (2, 2), (50, 3), (104, 104), (97, 50)]:
        assert winding(polys, inside) != 0, inside
    for outside in [(50, 50), (6, 6), (-6, 50), (106, 106), (94, 50)]:
        assert winding(polys, outside) == 0, outside


def test_stroke_with_sharp_turns_never_negative():
    zigzag = [(0, 0), (10, 30), (20, 0), (30, 30), (40, 0)]
    for join in ("miter", "round", "bevel"):
        polys = stroke_polygons(zigzag, False, 6, cap="round", join=join)
        for x in range(-10, 50):
            for y in range(-10, 40):
                assert winding(polys, (x + 0.37, y + 0.53)) >= 0


def test_shape_to_polys_fill_and_stroke():
    sp = SubPath(parse_path_data("M0 0 H10 V10 H0 Z")[0].segments, True)
    shape = VectorShape([sp], fill=(1, 0, 0), stroke=(0, 0, 0), stroke_width=2)
    polys = shape_to_polys(shape, 0.01)
    assert [p.color for p in polys] == [(1, 0, 0), (0, 0, 0)]
    assert poly_area(polys[:1]) == pytest.approx(100)


def test_svg_writer_roundtrip():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="50" height="40"><path d="M10 10 L40 10 C40 30 10 30 10 10Z"/></svg>'
    doc = parse_svg(svg)
    again = parse_svg(shapes_to_svg(doc.shapes, 50, 40))
    a = flatten_subpath(doc.shapes[0].subpaths[0], 0.01)
    b = flatten_subpath(again.shapes[0].subpaths[0], 0.01)
    assert len(a) == len(b)
    for p, q in zip(a, b):
        assert p == pytest.approx(q, abs=1e-3)


# --------------------------------------------------------------------------
# Tracing
# --------------------------------------------------------------------------

np = pytest.importorskip("numpy")
from svg_to_mesh.core.tracer import TraceSettings, marching_squares, trace_image  # noqa: E402


def _image(mask, color=(0.1, 0.1, 0.1)):
    img = np.ones(mask.shape + (4,))
    img[mask, :3] = color
    return img


def test_marching_squares_holes():
    yy, xx = np.mgrid[0:60, 0:60]
    r = np.hypot(xx - 30, yy - 30)
    field = ((r < 20) & (r > 10)).astype(float)
    contours = marching_squares(field, 0.5)
    assert len(contours) == 2
    areas = sorted(abs(signed_area(c.tolist())) for c in contours)
    assert areas[0] == pytest.approx(math.pi * 10 ** 2, rel=0.08)
    assert areas[1] == pytest.approx(math.pi * 20 ** 2, rel=0.05)


def test_trace_square_gives_four_straight_edges():
    yy, xx = np.mgrid[0:100, 0:100]
    mask = (abs(xx - 50) < 30) & (abs(yy - 50) < 20)
    shapes, w, h = trace_image(_image(mask), TraceSettings())
    assert (w, h) == (100, 100)
    assert len(shapes) == 1 and len(shapes[0].subpaths) == 1
    segs = shapes[0].subpaths[0].segments
    assert len(segs) == 4
    xs = sorted({round(s[0][0]) for s in segs})
    ys = sorted({round(s[0][1]) for s in segs})
    assert xs == [21, 80] and ys == [31, 70]


def test_trace_circle_is_smooth_and_accurate():
    yy, xx = np.mgrid[0:200, 0:200]
    mask = np.hypot(xx - 100, yy - 100) < 60
    shapes, _w, _h = trace_image(_image(mask), TraceSettings())
    sp = shapes[0].subpaths[0]
    assert len(sp.segments) <= 8
    pts = flatten_subpath(sp, 0.01)
    radii = [math.hypot(x - 100.5, y - 100.5) for x, y in pts]
    assert sum(radii) / len(radii) == pytest.approx(60, abs=0.6)
    assert max(radii) - min(radii) < 1.5


def test_trace_colors_and_background():
    yy, xx = np.mgrid[0:80, 0:160]
    img = np.ones((80, 160, 4))
    img[(abs(xx - 40) < 25) & (abs(yy - 40) < 25), :3] = (1, 0, 0)
    img[np.hypot(xx - 120, yy - 40) < 25, :3] = (0, 0, 1)
    shapes, _w, _h = trace_image(img, TraceSettings(mode="COLORS", num_colors=3))
    colors = sorted(tuple(round(c) for c in s.fill) for s in shapes)
    assert colors == [(0, 0, 1), (1, 0, 0)]


def test_trace_alpha_mode():
    yy, xx = np.mgrid[0:50, 0:50]
    img = np.zeros((50, 50, 4))
    img[..., :3] = 1.0
    img[np.hypot(xx - 25, yy - 25) < 15, 3] = 1.0
    shapes, _w, _h = trace_image(img, TraceSettings())  # AUTO picks alpha
    assert len(shapes) == 1 and len(shapes[0].subpaths) == 1
