"""Raster image -> vector shapes ("auto-trace"), using only numpy.

Pipeline::

    pixels --(mode: alpha / brightness / colour clusters)--> scalar fields
           --(gaussian blur)--> smooth fields (keeps anti-aliasing info)
           --(marching squares, sub-pixel)--> closed contours
           --(despeckle, resample, corner detection, light smoothing)-->
           --(least-squares Bézier fitting between corners)--> VectorShapes

Input images are ``(height, width, 4)`` float arrays (RGBA 0..1) whose
**first row is the bottom row** (Blender's pixel order), so the resulting
coordinates are y-up pixel units.
"""

import math
from dataclasses import dataclass

import numpy as np

from .bezier_fit import fit_closed_smooth, fit_open
from .geometry import SubPath, VectorShape, line_segment, signed_area


@dataclass
class TraceSettings:
    mode: str = "AUTO"  # AUTO | ALPHA | BRIGHTNESS | COLORS
    threshold: float = 0.5
    auto_threshold: bool = True
    invert: bool = False
    num_colors: int = 4
    blur: float = 0.8  # gaussian sigma in pixels
    despeckle: float = 12.0  # minimum region area in pixels^2
    corner_angle: float = 60.0  # degrees of turning that count as a corner
    fit_error: float = 0.5  # max. deviation of the fitted curve, pixels
    smoothing: float = 1.5  # contour smoothing radius in pixels
    max_resolution: int = 2048  # bigger images are downsampled first
    keep_background: bool = False  # COLORS mode: also trace the background


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------


def luminance(rgb):
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def gaussian_blur(a, sigma):
    if sigma <= 0.05:
        return a
    radius = max(1, int(math.ceil(sigma * 3)))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    k = np.exp(-(x * x) / (2 * sigma * sigma))
    k /= k.sum()
    out = np.pad(a, radius, mode="edge").astype(np.float64)
    # separable convolution by summing shifted copies (fast for small kernels)
    tmp = np.zeros((out.shape[0], a.shape[1]))
    for i, w in enumerate(k):
        tmp += w * out[:, i:i + a.shape[1]]
    res = np.zeros(a.shape)
    for i, w in enumerate(k):
        res += w * tmp[i:i + a.shape[0], :]
    return res


def otsu_threshold(values, bins=256):
    hist, edges = np.histogram(values, bins=bins, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    centers = (edges[:-1] + edges[1:]) * 0.5
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centers)
    mean0 = np.divide(m0, w0, out=np.zeros_like(m0), where=w0 > 0)
    mean1 = np.divide(m0[-1] - m0, w1, out=np.zeros_like(m0), where=w1 > 0)
    between = w0 * w1 * (mean0 - mean1) ** 2
    return float(centers[int(np.argmax(between))])


def downsample(img, factor):
    if factor <= 1:
        return img
    h, w = img.shape[:2]
    ph, pw = (-h) % factor, (-w) % factor
    pad = [(0, ph), (0, pw)] + [(0, 0)] * (img.ndim - 2)
    img = np.pad(img, pad, mode="edge")
    h2, w2 = img.shape[0] // factor, img.shape[1] // factor
    return img.reshape(h2, factor, w2, factor, *img.shape[2:]).mean(axis=(1, 3))


def border_values(a):
    return np.concatenate([a[0, :], a[-1, :], a[:, 0], a[:, -1]])


def kmeans(data, k, iterations=20, seed=0):
    """Plain k-means with k-means++ initialisation.  data: (n, d)."""
    rng = np.random.default_rng(seed)
    n = len(data)
    k = max(1, min(k, n))
    centers = [data[rng.integers(n)]]
    d2 = ((data - centers[0]) ** 2).sum(axis=1)
    for _ in range(1, k):
        total = d2.sum()
        if total <= 0:
            break
        idx = rng.choice(n, p=d2 / total)
        centers.append(data[idx])
        d2 = np.minimum(d2, ((data - data[idx]) ** 2).sum(axis=1))
    centers = np.array(centers)
    for _ in range(iterations):
        labels = assign_labels(data, centers)
        new = np.array([data[labels == i].mean(axis=0) if np.any(labels == i) else centers[i] for i in range(len(centers))])
        if np.allclose(new, centers, atol=1e-5):
            centers = new
            break
        centers = new
    return centers


def assign_labels(data, centers, chunk=262144):
    out = np.empty(len(data), dtype=np.int32)
    for s in range(0, len(data), chunk):
        part = data[s:s + chunk]
        d = ((part[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        out[s:s + chunk] = np.argmin(d, axis=1)
    return out


# --------------------------------------------------------------------------
# Marching squares
# --------------------------------------------------------------------------

# corners: 0=(i,j) 1=(i,j+1) 2=(i+1,j+1) 3=(i+1,j); positions as (x=col, y=row)
_CORNER_POS = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
# edges: 0=top(c0-c1) 1=right(c1-c2) 2=bottom(c2-c3) 3=left(c3-c0)
_EDGE_CORNERS = [(0, 1), (1, 2), (2, 3), (3, 0)]
_EDGE_MID = [(0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)]


def _orient(e1, e2, inside):
    """Order a segment so that the inside lies on its positive (cross>0) side."""
    m1, m2 = _EDGE_MID[e1], _EDGE_MID[e2]
    shared = set(_EDGE_CORNERS[e1]) & set(_EDGE_CORNERS[e2])
    c = shared.pop() if shared else 0
    cp = _CORNER_POS[c]
    cross = (m2[0] - m1[0]) * (cp[1] - m1[1]) - (m2[1] - m1[1]) * (cp[0] - m1[0])
    return (e1, e2) if (cross > 0) == bool(inside[c]) else (e2, e1)


def _build_table():
    table = {}
    for case in range(1, 15):
        inside = [(case >> b) & 1 for b in range(4)]
        crossing = [e for e in range(4) if inside[_EDGE_CORNERS[e][0]] != inside[_EDGE_CORNERS[e][1]]]
        if len(crossing) == 2:
            seg = [_orient(crossing[0], crossing[1], inside)]
            table[case] = (seg, seg)
        elif case == 5:  # c0, c2 inside
            out_c = [_orient(3, 0, inside), _orient(1, 2, inside)]  # isolate c0, c2
            in_c = [_orient(0, 1, inside), _orient(2, 3, inside)]  # isolate c1, c3
            table[case] = (out_c, in_c)
        elif case == 10:  # c1, c3 inside
            out_c = [_orient(0, 1, inside), _orient(2, 3, inside)]
            in_c = [_orient(3, 0, inside), _orient(1, 2, inside)]
            table[case] = (out_c, in_c)
    return table


_TABLE = _build_table()


def marching_squares(field, iso):
    """Closed iso-contours of *field* as a list of (n, 2) arrays (x, y).

    Coordinates are in pixel units with pixel centres at +0.5.
    """
    low = min(float(field.min()), iso) - 1.0
    f = np.pad(np.asarray(field, dtype=np.float64), 1, constant_values=low)
    H, W = f.shape
    inside = f > iso
    case = (
        inside[:-1, :-1].astype(np.uint8)
        | (inside[:-1, 1:].astype(np.uint8) << 1)
        | (inside[1:, 1:].astype(np.uint8) << 2)
        | (inside[1:, :-1].astype(np.uint8) << 3)
    )
    ii, jj = np.nonzero((case != 0) & (case != 15))
    if len(ii) == 0:
        return []
    cases = case[ii, jj]
    center = (f[ii, jj] + f[ii, jj + 1] + f[ii + 1, jj + 1] + f[ii + 1, jj]) * 0.25 > iso
    HW = H * W

    def key(e, i, j):
        if e == 0:
            return i * W + j
        if e == 1:
            return HW + i * W + j + 1
        if e == 2:
            return (i + 1) * W + j
        return HW + i * W + j

    froms, tos = [], []
    for c, (segs_out, segs_in) in _TABLE.items():
        sel_c = cases == c
        if not np.any(sel_c):
            continue
        variants = ((segs_out, sel_c & ~center), (segs_in, sel_c & center)) if c in (5, 10) else ((segs_out, sel_c),)
        for segs, sel in variants:
            if not np.any(sel):
                continue
            si, sj = ii[sel], jj[sel]
            for e1, e2 in segs:
                froms.append(key(e1, si, sj))
                tos.append(key(e2, si, sj))
    froms = np.concatenate(froms)
    tos = np.concatenate(tos)

    # sub-pixel positions of every crossing
    is_h = froms < HW
    k = np.where(is_h, froms, froms - HW)
    i, j = k // W, k % W
    i2 = np.where(is_h, i, i + 1)
    j2 = np.where(is_h, j + 1, j)
    fa, fb = f[i, j], f[i2, j2]
    t = (iso - fa) / (fb - fa)
    px = np.where(is_h, j + t, j) - 0.5
    py = np.where(is_h, i, i + t) - 0.5
    pos = dict(zip(froms.tolist(), zip(px.tolist(), py.tolist())))

    nxt = dict(zip(froms.tolist(), tos.tolist()))
    visited = set()
    contours = []
    for start in froms.tolist():
        if start in visited:
            continue
        loop = []
        cur = start
        while cur not in visited and cur in nxt:
            visited.add(cur)
            loop.append(pos[cur])
            cur = nxt[cur]
        if len(loop) >= 3:
            contours.append(np.array(loop))
    return contours


# --------------------------------------------------------------------------
# Contour -> Bézier path
# --------------------------------------------------------------------------


def resample_closed(pts, spacing):
    closed = np.vstack([pts, pts[:1]])
    seg = np.hypot(*np.diff(closed, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]
    n = max(8, int(round(total / spacing)))
    t = np.linspace(0.0, total, n, endpoint=False)
    return np.stack([np.interp(t, s, closed[:, 0]), np.interp(t, s, closed[:, 1])], axis=1)


def turning_angles(pts, reach):
    prev = np.roll(pts, reach, axis=0)
    nxt = np.roll(pts, -reach, axis=0)
    a = pts - prev
    b = nxt - pts
    na = np.hypot(a[:, 0], a[:, 1]) + 1e-12
    nb = np.hypot(b[:, 0], b[:, 1]) + 1e-12
    cosang = np.clip((a * b).sum(axis=1) / (na * nb), -1.0, 1.0)
    return np.degrees(np.arccos(cosang))


def detect_corners(pts, angle, reach):
    n = len(pts)
    if n < 2 * reach + 2:
        return []
    turn = turning_angles(pts, reach)
    cand = np.nonzero(turn > angle)[0]
    corners = []
    for idx in cand:
        lo, hi = idx - reach, idx + reach + 1
        window = turn[np.arange(lo, hi) % n]
        if turn[idx] >= window.max() - 1e-9:
            if not corners or (idx - corners[-1]) > reach:
                corners.append(int(idx))
    if len(corners) > 1 and (corners[0] + n - corners[-1]) <= reach:
        corners.pop()
    return corners


def smooth_closed(pts, fixed, sigma_samples):
    """Laplacian smoothing (~ gaussian of *sigma_samples*) with fixed corners."""
    iterations = int(round((sigma_samples ** 2) / 0.5))
    if iterations <= 0:
        return pts
    pts = pts.copy()
    mask = np.ones(len(pts), dtype=bool)
    mask[fixed] = False
    for _ in range(iterations):
        avg = (np.roll(pts, 1, axis=0) + np.roll(pts, -1, axis=0)) * 0.5
        pts[mask] += 0.5 * (avg[mask] - pts[mask])
    return pts


def _fit_line(run, trim):
    """Least-squares line (point, direction) through the inner part of a run."""
    inner = run[trim:len(run) - trim] if len(run) > 2 * trim + 2 else run
    c = inner.mean(axis=0)
    _u, _s, vt = np.linalg.svd(inner - c)
    return c, vt[0]


def _line_dist(run, line, trim):
    inner = run[trim:len(run) - trim] if len(run) > 2 * trim + 2 else run
    (c, d) = line
    return float(np.max(np.abs((inner[:, 0] - c[0]) * d[1] - (inner[:, 1] - c[1]) * d[0])))


def _intersect(l1, l2):
    (p, d), (q, e) = l1, l2
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 0.2:  # nearly parallel (< ~11 degrees)
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return p + d * t


SAMPLE = 0.5  # contour resampling distance in pixels


def contour_to_subpath(pts, settings, scale=1.0):
    pts = resample_closed(pts, SAMPLE)
    reach = 5  # samples (= 2.5 px)
    corners = detect_corners(pts, settings.corner_angle, reach)
    pts = smooth_closed(pts, corners, settings.smoothing / SAMPLE)
    err = max(0.05, settings.fit_error)
    segs = []
    if not corners:
        segs = fit_closed_smooth([tuple(p) for p in pts.tolist()], err, tangent_reach=6)
    else:
        n = len(pts)
        runs = [pts[np.arange(a, b + 1) % n] for a, b in zip(corners, corners[1:] + [corners[0] + n])]
        lines = [_fit_line(r, 4) if len(r) >= 6 else None for r in runs]
        is_line = [ln is not None and _line_dist(r, ln, 4) <= err for r, ln in zip(runs, lines)]
        # sharpen corners between two straight edges (undo the blur rounding)
        corner_pos = []
        for k in range(len(runs)):
            prev = k - 1
            p = runs[k][0]
            if is_line[k] and is_line[prev]:
                x = _intersect(lines[prev], lines[k])
                if x is not None and math.hypot(x[0] - p[0], x[1] - p[1]) < 4.0:
                    p = x
            corner_pos.append(p)
        for k, run in enumerate(runs):
            a = corner_pos[k]
            b = corner_pos[(k + 1) % len(runs)]
            if is_line[k]:
                segs.append(line_segment((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
            else:
                run = run.copy()
                run[0], run[-1] = a, b
                segs.extend(fit_open([tuple(p) for p in run.tolist()], err, tangent_reach=6))
    if not segs:
        return None
    if scale != 1.0:
        segs = [tuple((x * scale, y * scale) for x, y in seg) for seg in segs]
    return SubPath(segs, True)


# --------------------------------------------------------------------------
# Layers (what to trace)
# --------------------------------------------------------------------------


def build_layers(rgba, s):
    """Return a list of (field, iso, rgb_color, name)."""
    rgb = rgba[..., :3]
    alpha = rgba[..., 3]
    has_alpha = float(alpha.min()) < 0.5 and float((alpha < 0.5).mean()) > 0.001
    mode = s.mode
    if mode == "AUTO":
        mode = "ALPHA" if has_alpha else "BRIGHTNESS"

    if mode == "ALPHA":
        field = gaussian_blur(alpha, s.blur)
        iso = s.threshold if not s.auto_threshold else 0.5
        if s.invert:
            field, iso = 1.0 - field, 1.0 - iso
        color = _mean_color(rgb, field > iso)
        return [(field, iso, color, "Alpha")]

    if mode == "BRIGHTNESS":
        lum = luminance(rgb) * alpha + (1.0 - alpha)  # composite on white
        lum = gaussian_blur(lum, s.blur)
        thr = otsu_threshold(lum.ravel()) if s.auto_threshold else s.threshold
        field, iso = 1.0 - lum, 1.0 - thr  # dark = foreground
        invert = s.invert
        if s.mode == "AUTO" or s.auto_threshold:
            # the colour that dominates the image border is the background
            if float((border_values(field) > iso).mean()) > 0.5:
                invert = not invert
        if invert:
            field, iso = lum, thr
        color = _mean_color(rgb, field > iso)
        return [(field, iso, color, "Shape")]

    # COLORS
    opaque = alpha >= 0.5
    data = rgb[opaque]
    if len(data) == 0:
        return []
    sample = data
    if len(sample) > 60000:
        sample = sample[np.random.default_rng(1).choice(len(sample), 60000, replace=False)]
    centers = kmeans(sample, max(1, int(s.num_colors)))
    labels = np.full(alpha.shape, -1, dtype=np.int32)
    labels[opaque] = assign_labels(data, centers)
    k = len(centers)
    if has_alpha:
        background = -1
    else:
        bvals = border_values(labels)
        background = int(np.bincount(bvals, minlength=k).argmax())
    ids = list(range(k)) + ([-1] if has_alpha else [])
    blurred = {c: gaussian_blur((labels == c).astype(np.float64), max(s.blur, 0.5)) for c in ids}
    layers = []
    for c in range(k):
        if c == background and not s.keep_background:
            continue
        others = [blurred[o] for o in ids if o != c]
        field = blurred[c] - (np.max(others, axis=0) if others else 0.0)
        area = float((labels == c).sum())
        hexcol = "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in np.clip(centers[c], 0, 1))
        layers.append((area, field, 0.0, tuple(float(v) for v in centers[c]), "Color " + hexcol))
    layers.sort(key=lambda t: -t[0])  # big areas first, details painted on top
    return [(f, iso, col, name) for _a, f, iso, col, name in layers]


def _mean_color(rgb, mask):
    if np.any(mask):
        return tuple(float(v) for v in rgb[mask].mean(axis=0))
    return (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def trace_image(rgba, settings=None):
    """Trace an RGBA float image (rows bottom-up). Returns (shapes, w, h)."""
    s = settings or TraceSettings()
    rgba = np.asarray(rgba, dtype=np.float64)
    if rgba.ndim == 2:
        rgba = np.dstack([rgba, rgba, rgba, np.ones_like(rgba)])
    elif rgba.shape[2] == 3:
        rgba = np.dstack([rgba, np.ones(rgba.shape[:2])])
    elif rgba.shape[2] == 2:
        rgba = np.dstack([rgba[..., 0], rgba[..., 0], rgba[..., 0], rgba[..., 1]])
    h, w = rgba.shape[:2]
    factor = 1
    if s.max_resolution and max(h, w) > s.max_resolution:
        factor = int(math.ceil(max(h, w) / float(s.max_resolution)))
        rgba = downsample(rgba, factor)

    shapes = []
    min_area = s.despeckle / float(factor * factor)
    for field, iso, color, name in build_layers(rgba, s):
        subpaths = []
        for c in marching_squares(field, iso):
            if abs(signed_area(c.tolist())) < max(min_area, 0.5):
                continue
            sp = contour_to_subpath(c, s, scale=float(factor))
            if sp is not None:
                subpaths.append(sp)
        if subpaths:
            shapes.append(VectorShape(subpaths=subpaths, fill=color, fill_rule="evenodd", name=name))
    return shapes, w, h
