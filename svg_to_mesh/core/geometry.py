"""Shared 2D geometry: the vector shape model, Bézier flattening,
polygon clean-up and stroke outlining.

Coordinates are plain ``(x, y)`` tuples.  Every curve is stored as a list of
cubic Bézier segments ``(p0, c1, c2, p3)``; straight lines are cubics whose
control points coincide with their end points, so one code path handles all.
"""

import math
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class SubPath:
    segments: list  # list of (p0, c1, c2, p3)
    closed: bool = False

    def start(self):
        return self.segments[0][0]


@dataclass
class VectorShape:
    """One filled and/or stroked shape (an SVG element, a traced color...)."""

    subpaths: list = field(default_factory=list)
    fill: tuple = (0.0, 0.0, 0.0)  # sRGB 0..1, or None for no fill
    fill_rule: str = "nonzero"  # "nonzero" | "evenodd"
    stroke: tuple = None  # sRGB 0..1, or None
    stroke_width: float = 1.0
    linecap: str = "butt"
    linejoin: str = "miter"
    miterlimit: float = 4.0
    name: str = "Shape"


@dataclass
class PolyShape:
    """A shape after flattening: closed polygons plus fill rule."""

    contours: list  # list of list of (x, y)
    fill_rule: str = "nonzero"
    color: tuple = None
    name: str = "Shape"
    source_index: int = 0


def line_segment(a, b):
    return (a, a, b, b)


def is_line(seg, eps=1e-12):
    p0, c1, c2, p3 = seg
    return _dist_point_line(c1, p0, p3) <= eps and _dist_point_line(c2, p0, p3) <= eps


# --------------------------------------------------------------------------
# Affine transforms, SVG convention: (a, b, c, d, e, f)
#   x' = a*x + c*y + e
#   y' = b*x + d*y + f
# --------------------------------------------------------------------------

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m1, m2):
    """Return m1 * m2 (apply m2 first, then m1)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def mat_apply(m, p):
    a, b, c, d, e, f = m
    x, y = p
    return (a * x + c * y + e, b * x + d * y + f)


def mat_scale_factor(m):
    """Average linear scale of a transform (used for stroke widths)."""
    a, b, c, d, _e, _f = m
    return math.sqrt(abs(a * d - b * c))


def transform_shape(shape, m):
    for sp in shape.subpaths:
        sp.segments = [tuple(mat_apply(m, p) for p in seg) for seg in sp.segments]
    shape.stroke_width *= mat_scale_factor(m)
    return shape


def shapes_bounds(shapes):
    """Bounding box of all control points (contains the curves)."""
    xs, ys = [], []
    for s in shapes:
        for sp in s.subpaths:
            for seg in sp.segments:
                for x, y in seg:
                    xs.append(x)
                    ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------
# Flattening
# --------------------------------------------------------------------------


def _dist_point_line(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-300:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    return abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / length


def flatten_cubic(seg, tol, out, depth=0):
    """Adaptive subdivision; appends points (excluding p0) to *out*.

    The control polygon of a cubic deviates at most 3/4 of its control point
    distance from the chord, which gives a cheap, conservative flatness test.
    """
    p0, c1, c2, p3 = seg
    d = max(_dist_point_line(c1, p0, p3), _dist_point_line(c2, p0, p3))
    if d * 0.75 <= tol or depth >= 18:
        out.append(p3)
        return
    # de Casteljau split at t = 0.5
    m01 = ((p0[0] + c1[0]) * 0.5, (p0[1] + c1[1]) * 0.5)
    m12 = ((c1[0] + c2[0]) * 0.5, (c1[1] + c2[1]) * 0.5)
    m23 = ((c2[0] + p3[0]) * 0.5, (c2[1] + p3[1]) * 0.5)
    m012 = ((m01[0] + m12[0]) * 0.5, (m01[1] + m12[1]) * 0.5)
    m123 = ((m12[0] + m23[0]) * 0.5, (m12[1] + m23[1]) * 0.5)
    mid = ((m012[0] + m123[0]) * 0.5, (m012[1] + m123[1]) * 0.5)
    flatten_cubic((p0, m01, m012, mid), tol, out, depth + 1)
    flatten_cubic((mid, m123, m23, p3), tol, out, depth + 1)


def flatten_subpath(sp, tol):
    if not sp.segments:
        return []
    pts = [sp.segments[0][0]]
    for seg in sp.segments:
        flatten_cubic(seg, tol, pts)
    return pts


# --------------------------------------------------------------------------
# Polygon utilities
# --------------------------------------------------------------------------


def signed_area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a * 0.5


def clean_polygon(pts, eps, closed=True):
    """Remove duplicate and collinear points.

    *eps* is an absolute distance: points closer than eps to their
    predecessor are dropped, as are points deviating less than eps*0.25
    from the line through their neighbours.
    """
    if not pts:
        return []
    out = [pts[0]]
    for p in pts[1:]:
        q = out[-1]
        if abs(p[0] - q[0]) > eps or abs(p[1] - q[1]) > eps:
            if math.hypot(p[0] - q[0], p[1] - q[1]) > eps:
                out.append(p)
    if closed and len(out) > 1:
        if math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= eps:
            out.pop()
    # collinear removal (repeat until stable, bounded)
    lim = eps * 0.25
    for _ in range(4):
        n = len(out)
        if n < 3:
            break
        keep = []
        changed = False
        rng = range(n) if closed else range(n)
        for i in rng:
            if not closed and (i == 0 or i == n - 1):
                keep.append(out[i])
                continue
            a = keep[-1] if keep else out[i - 1]
            b = out[(i + 1) % n]
            if _dist_point_line(out[i], a, b) <= lim and _between(out[i], a, b):
                changed = True
                continue
            keep.append(out[i])
        out = keep
        if not changed:
            break
    return out


def _between(p, a, b):
    """True if p's projection lies between a and b (no spikes removed)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = (p[0] - a[0]) * dx + (p[1] - a[1]) * dy
    return 0.0 <= t <= dx * dx + dy * dy


def simplify_dp(pts, tol, closed=True):
    """Douglas-Peucker simplification."""
    n = len(pts)
    if n < 4:
        return list(pts)
    if closed:
        # split at the point farthest from pts[0]
        far = max(range(n), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
        a = _dp(pts[: far + 1], tol)
        b = _dp(pts[far:] + [pts[0]], tol)
        return a[:-1] + b[:-1]
    return _dp(pts, tol)


def _dp(pts, tol):
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            d = _dist_point_line(pts[k], pts[i], pts[j])
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def subdivide_polygon(pts, max_len):
    """Insert points so that no edge is longer than *max_len* (corners kept)."""
    n = len(pts)
    out = []
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        out.append(a)
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        k = int(math.ceil(length / max_len)) if max_len > 0 else 1
        for s in range(1, k):
            t = s / k
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


# --------------------------------------------------------------------------
# Stroke outlining
#
# Every stroked sub-path becomes one outline polygon (two for closed paths):
# the right offset forwards, the left offset backwards.  Outer joins get
# miter / round / bevel points, inner joins run through the centre vertex
# ("pivot").  Such an outline has the same winding numbers as the union of
# one rectangle per segment plus join wedges, i.e. it is never negative, so
# filling it with the non-zero rule gives the exact stroke area even where
# the outline self-intersects.  The triangulation resolves those crossings.
# --------------------------------------------------------------------------


def _arc_points(c, r, a0, a1, tol):
    """Points on a circular arc from angle a0 to a1 (radians, any direction)."""
    if r <= 0:
        return [c]
    step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - tol / r))) if tol < r else math.pi / 2
    step = max(step, math.pi / 64)
    n = max(1, int(math.ceil(abs(a1 - a0) / step)))
    return [(c[0] + r * math.cos(a0 + (a1 - a0) * i / n), c[1] + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def _offset_side(pts, dirs, closed, side, h, join, miterlimit, tol):
    """Offset polyline on one side (+1 = left, -1 = right), forward order."""
    n = len(pts)
    seg_count = len(dirs)
    out = []
    for i in range(seg_count):
        a = pts[i]
        b = pts[(i + 1) % n]
        d = dirs[i]
        N = (-d[1] * h * side, d[0] * h * side)
        if i > 0 or closed:
            dp = dirs[i - 1]
            Np = (-dp[1] * h * side, dp[0] * h * side)
            cross = dp[0] * d[1] - dp[1] * d[0]
            dot = dp[0] * d[0] + dp[1] * d[1]
            p_in = (a[0] + Np[0], a[1] + Np[1])
            p_out = (a[0] + N[0], a[1] + N[1])
            if abs(cross) < 1e-12 and dot > 0:
                out.append(p_out)
            elif (cross > 0) == (side > 0):  # inner side of the turn
                out.extend((p_in, a, p_out))
            elif join == "round":
                a0 = math.atan2(Np[1], Np[0])
                a1 = math.atan2(N[1], N[0])
                if cross > 0:
                    while a1 < a0:
                        a1 += 2 * math.pi
                else:
                    while a1 > a0:
                        a1 -= 2 * math.pi
                out.extend(_arc_points(a, h, a0, a1, tol))
            else:
                out.append(p_in)
                if join in ("miter", "miter-clip", "arcs"):
                    half = (math.pi - math.acos(max(-1.0, min(1.0, dot)))) * 0.5
                    bis = (Np[0] + N[0], Np[1] + N[1])
                    bl = math.hypot(*bis)
                    if half > 1e-9 and bl > 1e-12 and 1.0 / math.sin(half) <= miterlimit:
                        m = h / math.sin(half)
                        out.append((a[0] + bis[0] / bl * m, a[1] + bis[1] / bl * m))
                out.append(p_out)
        else:
            out.append((a[0] + N[0], a[1] + N[1]))
        out.append((b[0] + N[0], b[1] + N[1]))
    return out


def _cap(p, d, h, cap, tol, at_end):
    """Points connecting the right side to the left side (end) or back."""
    sgn = 1.0 if at_end else -1.0
    ux, uy = d[0] * sgn, d[1] * sgn  # pointing away from the path
    right = (p[0] + uy * h, p[1] - ux * h)
    left = (p[0] - uy * h, p[1] + ux * h)
    if cap == "square":
        return [right, (right[0] + ux * h, right[1] + uy * h), (left[0] + ux * h, left[1] + uy * h), left]
    if cap == "round":
        a0 = math.atan2(-ux, uy)  # direction of 'right' relative to p
        return _arc_points(p, h, a0, a0 + math.pi, tol)
    return [right, left]


def stroke_polygons(pts, closed, width, cap="butt", join="miter", miterlimit=4.0, tol=0.01):
    h = width * 0.5
    if h <= 0 or len(pts) < 2:
        return []
    pts = clean_polygon(pts, tol * 0.01, closed=closed)
    n = len(pts)
    if n < 2 or (closed and n < 3):
        closed = False
        if n < 2:
            return []
    seg_count = n if closed else n - 1
    dirs = []
    for i in range(seg_count):
        a = pts[i]
        b = pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        dirs.append((dx / length, dy / length) if length > 0 else (1.0, 0.0))
    right = _offset_side(pts, dirs, closed, -1.0, h, join, miterlimit, tol)
    left = _offset_side(pts, dirs, closed, 1.0, h, join, miterlimit, tol)
    if closed:
        polys = [right, left[::-1]]
    else:
        poly = right[:-1] + _cap(pts[-1], dirs[-1], h, cap, tol, True) + left[::-1][1:-1]
        poly += _cap(pts[0], dirs[0], h, cap, tol, False)
        polys = [poly]
    polys = [clean_polygon(p, tol * 0.01, closed=True) for p in polys]
    return [p for p in polys if len(p) >= 3]


# --------------------------------------------------------------------------
# Shape -> polygons
# --------------------------------------------------------------------------


def shape_to_polys(shape, tol, include_fill=True, include_stroke=True):
    """Flatten a VectorShape into PolyShapes (fill and/or stroke)."""
    result = []
    if include_fill and shape.fill is not None:
        contours = []
        for sp in shape.subpaths:
            pts = flatten_subpath(sp, tol)
            pts = clean_polygon(pts, tol * 0.05, closed=True)
            if len(pts) >= 3:
                contours.append(pts)
        if contours:
            result.append(PolyShape(contours, shape.fill_rule, shape.fill, shape.name))
    if include_stroke and shape.stroke is not None and shape.stroke_width > 0:
        contours = []
        for sp in shape.subpaths:
            pts = flatten_subpath(sp, tol)
            closed = sp.closed
            if closed and len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]
            contours.extend(
                stroke_polygons(pts, closed, shape.stroke_width, shape.linecap, shape.linejoin, shape.miterlimit, tol)
            )
        if contours:
            result.append(PolyShape(contours, "nonzero", shape.stroke, shape.name + "_stroke"))
    return result
