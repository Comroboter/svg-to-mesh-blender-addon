"""Polygons -> clean Blender meshes.

All shapes are triangulated together with one Constrained Delaunay
Triangulation (``mathutils.geometry.delaunay_2d_cdt``).  Every outline is a
constraint, so intersections between shapes are resolved exactly.  Each
triangle then gets a winding number per shape through a flood fill over the
triangle adjacency: crossing a constraint edge changes the winding of the
edge's shape by +-1.  With that, fill rules (non-zero / even-odd), unions of
overlapping shapes and "what is visible on top" (painter's order) are exact
and cheap, and the result has no gaps, overlaps or duplicate vertices.

The triangles are then turned into the requested topology (clean n-gons,
triangles, uniform triangles or quads) and optionally extruded into a closed,
manifold solid – ready for booleans.
"""

import math
from collections import deque
from dataclasses import dataclass

import bmesh
from mathutils import Vector
from mathutils.geometry import delaunay_2d_cdt, tessellate_polygon
from mathutils.kdtree import KDTree

from .core.geometry import PolyShape, clean_polygon, signed_area, subdivide_polygon


@dataclass
class MeshSettings:
    topology: str = "NGON"  # NGON | TRIS | UNIFORM | QUADS
    grid_size: float = 0.02  # UNIFORM/QUADS cell size (absolute units)
    depth: float = 0.0  # extrusion (absolute units), 0 = flat
    center_depth: bool = False
    overlap: str = "VISIBLE"  # VISIBLE | UNION
    merge_distance: float = 1e-6  # absolute units
    smooth_quads: int = 4


@dataclass
class TriangulationResult:
    verts: list  # list of (x, y)
    faces: list  # list of (i, j, k), counter-clockwise
    groups: list  # per face: set of group ids


AUTO = "AUTO"  # knockout value: a hole only where the shape is background


def _background_shapes(faces, adjacency, covers, knockout, shown):
    """AUTO knockout shapes (white) that reach the outside of the drawing.

    Starting from empty triangles, walk through triangles that show nothing
    but AUTO shapes; every AUTO shape met on the way is background (a white
    rectangle behind a logo, including the parts seen through letter holes),
    while white enclosed by other colors (eyes, belly, white letters) is not.
    """
    def only_auto(fi):
        vis = shown(covers[fi])
        return bool(vis) and all(knockout[s] == AUTO for s in vis)

    if not any(k == AUTO for k in knockout):
        return set()
    empty = [fi for fi, cov in enumerate(covers) if not any(not knockout[s] or knockout[s] == AUTO for s in shown(cov))]
    seen = set(empty)
    queue = deque(empty)
    background = set()
    while queue:
        fi = queue.popleft()
        a, b, c = faces[fi]
        for u, v in ((a, b), (b, c), (c, a)):
            for gi in adjacency[(u, v) if u < v else (v, u)]:
                if gi not in seen and only_auto(gi):
                    seen.add(gi)
                    background.update(shown(covers[gi]))
                    queue.append(gi)
    return background


def _filled(rule, w):
    return (w & 1) == 1 if rule == "evenodd" else w != 0


def triangulate(poly_shapes, group_of, knockout, settings, uniform=False):
    """Triangulate all shapes together and label triangles with groups.

    poly_shapes: list of PolyShape (paint order).
    group_of:    group id per shape.
    knockout:    per shape, True if the shape cuts away what lies below it,
                 AUTO if it only does so where it is background (see
                 _background_shapes).

    Clip paths (PolyShape.clips) are added as extra, invisible shapes: a shape
    only covers a triangle if its own fill rule holds there and every one of
    its clip groups has at least one filled clip shape there.
    """
    eps = max(settings.merge_distance, 1e-9)
    shapes = list(poly_shapes)
    n_painted = len(shapes)
    clip_index = {}  # id(group) -> list of aux shape indices (shared groups are added once)
    clip_groups = []
    for shp in poly_shapes:
        groups = []
        for group in getattr(shp, "clips", ()) or ():
            key = id(group)
            if key not in clip_index:
                clip_index[key] = list(range(len(shapes), len(shapes) + len(group)))
                shapes.extend(group)
            groups.append(clip_index[key])
        clip_groups.append(groups)

    verts = []
    edges = []
    edge_shape = []
    xs, ys = [], []
    uniform = uniform and settings.grid_size > 0
    for si, shp in enumerate(shapes):
        for contour in shp.contours:
            if uniform:
                contour = subdivide_polygon(contour, settings.grid_size)
            n = len(contour)
            if n < 3:
                continue
            base = len(verts)
            verts.extend(contour)
            for i in range(n):
                edges.append((base + i, base + (i + 1) % n))
                edge_shape.append(si)
    if not edges:
        return TriangulationResult([], [], [])
    for x, y in verts:
        xs.append(x)
        ys.append(y)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    size = max(maxx - minx, maxy - miny, eps)
    pad = size * 0.05 + eps * 10
    n_shape_verts = len(verts)

    # padding box: triangles touching it are guaranteed outside everything
    corners = [(minx - pad, miny - pad), (maxx + pad, miny - pad), (maxx + pad, maxy + pad), (minx - pad, maxy + pad)]
    corner_ids = set(range(len(verts), len(verts) + 4))
    verts.extend(corners)

    if uniform:
        verts.extend(_steiner_grid(verts[:n_shape_verts], minx, miny, maxx, maxy, settings.grid_size))

    out_verts, out_edges, out_faces, orig_verts, orig_edges, _orig_faces = delaunay_2d_cdt(
        [Vector(v) for v in verts], edges, [], 0, eps
    )
    ov = [(v.x, v.y) for v in out_verts]

    edge_index = {}
    for i, (a, b) in enumerate(out_edges):
        edge_index[(a, b) if a < b else (b, a)] = i

    # make triangles counter-clockwise. The CDT orients all its faces the
    # same way, so decide once for all of them: judging each triangle on its
    # own would flip near-degenerate slivers at random and break the
    # adjacency walks below.
    signed = 0.0
    for f in out_faces:
        (ax, ay), (bx, by), (cx, cy) = ov[f[0]], ov[f[1]], ov[f[2]]
        signed += (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if signed >= 0:
        faces = [(f[0], f[1], f[2]) for f in out_faces]
    else:
        faces = [(f[0], f[2], f[1]) for f in out_faces]

    adjacency = {}
    for fi, (a, b, c) in enumerate(faces):
        for u, v in ((a, b), (b, c), (c, a)):
            adjacency.setdefault((u, v) if u < v else (v, u), []).append(fi)

    corner_out = {i for i, origs in enumerate(orig_verts) if any(o in corner_ids for o in origs)}
    winding = [None] * len(faces)
    queue = deque()
    for fi, f in enumerate(faces):
        if corner_out.intersection(f):
            winding[fi] = {}
            queue.append(fi)

    while queue:
        fi = queue.popleft()
        a, b, c = faces[fi]
        w = winding[fi]
        cxm = (ov[a][0] + ov[b][0] + ov[c][0]) / 3.0
        cym = (ov[a][1] + ov[b][1] + ov[c][1]) / 3.0
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u < v else (v, u)
            for gi in adjacency[key]:
                if gi == fi or winding[gi] is not None:
                    continue
                ei = edge_index.get(key)
                origs = orig_edges[ei] if ei is not None else ()
                if not origs:
                    winding[gi] = w
                else:
                    nw = dict(w)
                    ex, ey = ov[key[1]][0] - ov[key[0]][0], ov[key[1]][1] - ov[key[0]][1]
                    side = ex * (cym - ov[key[0]][1]) - ey * (cxm - ov[key[0]][0])
                    for e in origs:
                        if e >= len(edge_shape):
                            continue
                        s = edge_shape[e]
                        p0, p1 = verts[edges[e][0]], verts[edges[e][1]]
                        same_dir = (p1[0] - p0[0]) * ex + (p1[1] - p0[1]) * ey >= 0
                        left = (side > 0) == same_dir  # current triangle left of the input edge?
                        nw[s] = nw.get(s, 0) + (-1 if left else 1)
                        if nw[s] == 0:
                            del nw[s]
                    winding[gi] = nw
                queue.append(gi)

    # per triangle: covering shapes (only painted ones whose clip paths allow it)
    covers = []
    for w in winding:
        if not w:
            covers.append([])
            continue
        filled = {s for s, n in w.items() if _filled(shapes[s].fill_rule, n)}
        covers.append(sorted(
            s for s in filled
            if s < n_painted and all(any(a in filled for a in grp) for grp in clip_groups[s])
        ))
    visible = settings.overlap == "VISIBLE"

    def shown(cov):
        return cov[-1:] if visible else cov

    background = _background_shapes(faces, adjacency, covers, knockout, shown)
    groups = []
    for cov in covers:
        g = set()
        for s in shown(cov):
            k = knockout[s]
            if not k or (k == AUTO and s not in background):
                g.add(group_of[s])
        groups.append(g)
    return TriangulationResult(ov, faces, groups)


def _steiner_grid(boundary_pts, minx, miny, maxx, maxy, step):
    """Regular grid points, keeping a distance from the outlines."""
    kd = KDTree(len(boundary_pts))
    for i, (x, y) in enumerate(boundary_pts):
        kd.insert((x, y, 0.0), i)
    kd.balance()
    out = []
    nx = int(math.floor((maxx - minx) / step))
    ny = int(math.floor((maxy - miny) / step))
    if (nx + 1) * (ny + 1) > 400000:
        return out
    ox = minx + ((maxx - minx) - nx * step) * 0.5
    oy = miny + ((maxy - miny) - ny * step) * 0.5
    min_d = step * 0.6
    for i in range(nx + 1):
        x = ox + i * step
        for j in range(ny + 1):
            y = oy + j * step
            _co, _idx, d = kd.find((x, y, 0.0))
            if d is not None and d > min_d:
                out.append((x, y))
    return out


# --------------------------------------------------------------------------
# bmesh construction
# --------------------------------------------------------------------------


def faces_by_group(tri):
    """Face indices per group id (one pass instead of one per group)."""
    out = {}
    for fi, g in enumerate(tri.groups):
        for gid in g:
            out.setdefault(gid, []).append(fi)
    return out


def _index_loops(tri, faces):
    """Boundary loops (vertex indices) of the triangles *faces*.

    Walking around each vertex through its triangle fan keeps regions that
    touch in a single point as separate loops.
    """
    third = {}
    for fi in faces:
        a, b, c = tri.faces[fi]
        third[(a, b)] = c
        third[(b, c)] = a
        third[(c, a)] = b
    boundary = [e for e in third if (e[1], e[0]) not in third]
    nxt = {}
    for u, v in boundary:
        cur = (v, third[(u, v)])
        for _ in range(100000):
            back = (cur[1], cur[0])
            if back not in third:
                break
            cur = (v, third[back])
        nxt[(u, v)] = cur
    loops = []
    seen = set()
    for e in boundary:
        if e in seen:
            continue
        loop = []
        cur = e
        while cur not in seen and cur in nxt:
            seen.add(cur)
            loop.append(cur[0])
            cur = nxt[cur]
        loops.append(loop)
    return loops


def _group_faces(tri, group, faces):
    return faces if faces is not None else [fi for fi, g in enumerate(tri.groups) if group in g]


def region_loops(tri, group, eps, faces=None):
    """Boundary loops of the triangles of *group* (outer CCW, holes CW).

    Vertices that only lie on a straight line (left over from hidden shapes)
    are removed.
    """
    loops = []
    for idx in _index_loops(tri, _group_faces(tri, group, faces)):
        loop = clean_polygon([tri.verts[i] for i in idx], eps, closed=True)
        if len(loop) >= 3 and abs(signed_area(loop)) > eps * eps:
            loops.append(loop)
    return loops


def winding_number(pt, loops):
    """Winding number of *pt* with respect to closed polygon loops."""
    x, y = pt
    w = 0
    for loop in loops:
        n = len(loop)
        for i in range(n):
            x1, y1 = loop[i]
            x2, y2 = loop[(i + 1) % n]
            if y1 <= y < y2 or y2 <= y < y1:
                cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
                if y1 <= y < y2 and cross > 0:
                    w += 1
                elif y2 <= y < y1 and cross < 0:
                    w -= 1
    return w


def _interior_point(coords):
    """A point strictly inside a simple polygon (centroid of its largest triangle)."""
    tris = tessellate_polygon([[Vector((x, y, 0.0)) for x, y in coords]])
    best, best_area = None, -1.0
    for a, b, c in tris:
        (ax, ay), (bx, by), (cx, cy) = coords[a], coords[b], coords[c]
        area = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))
        if area > best_area:
            best, best_area = ((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0), area
    if best is None:
        n = len(coords)
        best = (sum(x for x, _y in coords) / n, sum(y for _x, y in coords) / n)
    return best


def _ngons_from_loops(bm, loops, eps, settings):
    """Clean n-gons covering exactly the region bounded by *loops*.

    The CDT (output type 5) splits the loops into valid BMesh faces and
    bridges holes. Which faces belong to the region is decided here with
    the winding number of the loops (outer loops CCW, holes CW), because
    Blender's hole detection changed between versions (5.2 keeps hole faces
    that 5.0 removed). If the kept faces do not add up to the exact region
    area, the region is triangulated with our own CDT instead.
    """
    verts = []
    faces = []
    for loop in loops:
        base = len(verts)
        verts.extend(loop)
        faces.append(list(range(base, base + len(loop))))
    out_verts, _e, out_faces, _ov, _oe, _of = delaunay_2d_cdt([Vector(v) for v in verts], [], faces, 5, eps)
    co = [(v.x, v.y) for v in out_verts]
    kept = [f for f in out_faces if winding_number(_interior_point([co[i] for i in f]), loops) != 0]

    region_area = abs(sum(signed_area(loop) for loop in loops))
    kept_area = sum(abs(signed_area([co[i] for i in f])) for f in kept)
    if abs(kept_area - region_area) > 1e-4 * region_area + eps * eps:
        sub = triangulate([PolyShape(loops, "nonzero")], [0], [False], settings)
        _faces_from_tri(bm, sub, 0)
        return

    bverts = [None] * len(co)
    for f in kept:
        for i in f:
            if bverts[i] is None:
                bverts[i] = bm.verts.new((co[i][0], co[i][1], 0.0))
        try:
            bm.faces.new([bverts[i] for i in f])
        except ValueError:
            pass


def _faces_from_tri(bm, tri, group):
    vmap = {}
    for f, g in zip(tri.faces, tri.groups):
        if group not in g:
            continue
        bv = []
        for i in f:
            v = vmap.get(i)
            if v is None:
                x, y = tri.verts[i]
                v = vmap[i] = bm.verts.new((x, y, 0.0))
            bv.append(v)
        try:
            bm.faces.new(bv)
        except ValueError:
            pass


def _flat_faces(bm, loops, eps, settings):
    """Fill the region bounded by *loops* with faces in the chosen topology (z=0)."""
    if settings.topology == "NGON":
        _ngons_from_loops(bm, loops, eps, settings)
    else:
        # re-triangulate the clean outline (optionally with evenly spaced
        # inner points); constrained Delaunay gives well-shaped triangles
        region = PolyShape(loops, "nonzero")
        uniform = settings.topology in ("UNIFORM", "QUADS")
        sub = triangulate([region], [0], [False], settings, uniform=uniform)
        _faces_from_tri(bm, sub, 0)
        # (no dissolve_degenerate here: the CDT already merges vertices closer than eps,
        # and on hair-thin strips it removed valid triangles)
        if settings.topology == "QUADS":
            bmesh.ops.join_triangles(
                bm, faces=bm.faces[:], cmp_seam=False, cmp_sharp=False, cmp_uvs=False,
                cmp_vcols=False, cmp_materials=False,
                angle_face_threshold=math.radians(40.0), angle_shape_threshold=math.radians(40.0),
            )
        if uniform and settings.smooth_quads > 0:
            inner = [v for v in bm.verts if not v.is_boundary and all(len(e.link_faces) == 2 for e in v.link_edges)]
            for _ in range(settings.smooth_quads):
                bmesh.ops.smooth_vert(bm, verts=inner, factor=0.5, use_axis_x=True, use_axis_y=True, use_axis_z=False)
    _drop_slivers(bm, eps * 2.0)
    _remove_loose(bm)


def _drop_slivers(bm, dist):
    """Remove zero-height faces (a vertex lying on the opposite edge) and
    stitch the edge to that vertex instead, so the edge has two faces."""
    slivers = []
    for f in bm.faces:
        longest = max(e.calc_length() for e in f.edges)
        if longest > 0 and 2.0 * f.calc_area() / longest < dist:
            slivers.append(f)
    if slivers:
        bmesh.ops.delete(bm, geom=slivers, context="FACES_ONLY")
        _fix_t_junctions(bm, dist)


def _finish(bm, settings):
    for f in bm.faces:
        if f.normal.z < 0:
            f.normal_flip()
    if settings.depth > 0:
        solidify(bm, settings.depth)
        if settings.center_depth:
            bmesh.ops.translate(bm, verts=bm.verts[:], vec=(0.0, 0.0, -settings.depth * 0.5))
    bm.normal_update()
    return bm


def _group_flat(tri, group, faces, eps, settings):
    """Flat faces of one group. If the result does not cover exactly the area
    of the group's triangles (broken outline next to degenerate triangles),
    the group's triangles are used directly instead."""
    faces = _group_faces(tri, group, faces)
    bm = bmesh.new()
    loops = region_loops(tri, group, eps, faces)
    if loops:
        _flat_faces(bm, loops, eps, settings)
    exact = 0.0
    for fi in faces:
        a, b, c = (tri.verts[i] for i in tri.faces[fi])
        exact += abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) * 0.5
    area = sum(f.calc_area() for f in bm.faces)
    if abs(area - exact) > 1e-4 * exact + eps * eps:
        bm.free()
        bm = bmesh.new()
        _faces_from_tri(bm, tri, group)
        _drop_slivers(bm, eps * 2.0)
        _remove_loose(bm)
    return bm


def build_bmesh(tri, group, settings, faces=None):
    """Create a bmesh for one group of a TriangulationResult.

    *faces* optionally lists the face indices of the group (see faces_by_group).
    """
    eps = max(settings.merge_distance, 1e-9)
    bm = _group_flat(tri, group, faces, eps, settings)
    if not bm.faces:
        return bm
    return _finish(bm, settings)


def _fix_t_junctions(bm, dist):
    """Weld border vertices onto straight border edges they lie on (left
    over after removing sliver faces), so every edge has two faces again."""
    for _ in range(4):
        bverts = [v for v in bm.verts if v.is_boundary]
        if not bverts:
            return
        kd = KDTree(len(bverts))
        for i, v in enumerate(bverts):
            kd.insert(v.co, i)
        kd.balance()
        splits = []
        for e in bm.edges:
            if not e.is_boundary:
                continue
            a, b = e.verts
            d = b.co - a.co
            length = d.length
            if length <= 2 * dist:
                continue
            on_edge = []
            for co, i, _d in kd.find_range((a.co + b.co) * 0.5, length * 0.5 + dist):
                v = bverts[i]
                if v is a or v is b:
                    continue
                t = (co - a.co).dot(d) / (length * length)
                if dist < t * length < length - dist and (a.co + d * t - co).length <= dist:
                    on_edge.append((t, v))
            if on_edge:
                splits.append((e, a, b, sorted(on_edge, key=lambda x: x[0])))
        if not splits:
            return
        targetmap = {}
        for e, a, b, on_edge in splits:
            if not e.is_valid:
                continue
            cur, start, done = e, a, 0.0
            for t, v in on_edge:
                if v in targetmap or v.is_valid is False:
                    continue
                fac = (t - done) / (1.0 - done)
                _new_edge, nv = bmesh.utils.edge_split(cur, start, fac)
                targetmap[nv] = v
                cur = next((ed for ed in nv.link_edges if b in ed.verts), None)
                start, done = nv, t
                if cur is None:
                    break
        if not targetmap:
            return
        bmesh.ops.weld_verts(bm, targetmap=targetmap)


def _remove_loose(bm):
    wire = [e for e in bm.edges if not e.link_faces]
    if wire:
        bmesh.ops.delete(bm, geom=wire, context="EDGES")
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")


def solidify(bm, depth):
    """Turn a flat (z=0, +Z facing) surface into a closed manifold solid."""
    # Regions touching in a single point would give side edges with four
    # faces; give each face fan its own vertex so every edge stays manifold.
    for v in bm.verts[:]:
        if sum(1 for e in v.link_edges if e.is_boundary) > 2:
            bmesh.utils.vert_separate(v, [])
    bottom_faces = bm.faces[:]
    boundary = []
    for e in bm.edges:
        if len(e.link_faces) == 1:
            loop = e.link_loops[0]
            boundary.append((loop.vert, loop.link_loop_next.vert, loop.face.material_index))
    top = {v: bm.verts.new((v.co.x, v.co.y, depth)) for v in bm.verts[:]}
    for f in bottom_faces:
        nf = bm.faces.new([top[v] for v in f.verts])
        nf.material_index = f.material_index
        nf.smooth = f.smooth
    for a, b, mat in boundary:
        try:
            bm.faces.new((a, b, top[b], top[a])).material_index = mat
        except ValueError:
            pass
    bmesh.ops.reverse_faces(bm, faces=bottom_faces)
    bm.normal_update()


def add_planar_uvs(bm, bounds):
    minx, miny, maxx, maxy = bounds
    size = max(maxx - minx, maxy - miny, 1e-12)
    uv = bm.loops.layers.uv.verify()
    for f in bm.faces:
        for loop in f.loops:
            co = loop.vert.co
            loop[uv].uv = ((co.x - minx) / size, (co.y - miny) / size)
