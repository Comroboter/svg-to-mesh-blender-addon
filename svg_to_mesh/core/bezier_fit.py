"""Least-squares cubic Bézier fitting of point runs.

Implementation of Philip J. Schneider's algorithm ("An Algorithm for
Automatically Fitting Digitized Curves", Graphics Gems, 1990), vectorized
with numpy: every step works on all points of a run at once.
"""

import numpy as np


def _norm(v):
    length = float(np.hypot(v[0], v[1]))
    return v / length if length > 1e-12 else np.zeros(2)


def _bezier(ctrl, t):
    mt = 1.0 - t
    basis = np.stack([mt * mt * mt, 3.0 * mt * mt * t, 3.0 * mt * t * t, t * t * t], axis=1)
    return basis @ ctrl


def _bezier_d1(ctrl, t):
    q = 3.0 * (ctrl[1:] - ctrl[:-1])
    mt = 1.0 - t
    return np.stack([mt * mt, 2.0 * mt * t, t * t], axis=1) @ q


def _bezier_d2(ctrl, t):
    q = 3.0 * (ctrl[1:] - ctrl[:-1])
    r = 2.0 * (q[1:] - q[:-1])
    return np.stack([1.0 - t, t], axis=1) @ r


def _chord_params(pts):
    d = np.hypot(*np.diff(pts, axis=0).T)
    u = np.concatenate([[0.0], np.cumsum(d)])
    return u / (u[-1] if u[-1] > 0 else 1.0)


def _generate(pts, u, t1, t2):
    first, last = pts[0], pts[-1]
    mt = 1.0 - u
    b0, b1, b2, b3 = mt * mt * mt, 3.0 * u * mt * mt, 3.0 * u * u * mt, u * u * u
    a1 = b1[:, None] * t1
    a2 = b2[:, None] * t2
    c00 = float((a1 * a1).sum())
    c01 = float((a1 * a2).sum())
    c11 = float((a2 * a2).sum())
    tmp = pts - (np.outer(b0 + b1, first) + np.outer(b2 + b3, last))
    x0 = float((a1 * tmp).sum())
    x1 = float((a2 * tmp).sum())
    det = c00 * c11 - c01 * c01
    seg_len = float(np.hypot(*(last - first)))
    eps = 1e-6 * seg_len
    if abs(det) > 1e-12:
        alpha_l = (x0 * c11 - c01 * x1) / det
        alpha_r = (c00 * x1 - x0 * c01) / det
    else:
        alpha_l = alpha_r = 0.0
    if alpha_l < eps or alpha_r < eps:
        alpha_l = alpha_r = seg_len / 3.0
    return np.array([first, first + t1 * alpha_l, last + t2 * alpha_r, last])


def _max_error(pts, ctrl, u):
    n = len(pts)
    if n <= 2:
        return 0.0, n // 2
    d = ((_bezier(ctrl, u[1:-1]) - pts[1:-1]) ** 2).sum(axis=1)
    i = int(np.argmax(d))
    return float(d[i]), i + 1


def _reparam(pts, ctrl, u):
    d = _bezier(ctrl, u) - pts
    d1 = _bezier_d1(ctrl, u)
    d2 = _bezier_d2(ctrl, u)
    num = (d * d1).sum(axis=1)
    den = (d1 * d1).sum(axis=1) + (d * d2).sum(axis=1)
    ok = np.abs(den) > 1e-12
    nt = np.where(ok, u - num / np.where(ok, den, 1.0), u)
    return np.clip(nt, 0.0, 1.0)


def _as_segment(ctrl):
    return tuple((float(x), float(y)) for x, y in ctrl)


def _fit(pts, t1, t2, err_sq, out, depth=0):
    if len(pts) == 2:
        dist = float(np.hypot(*(pts[1] - pts[0]))) / 3.0
        out.append(_as_segment([pts[0], pts[0] + t1 * dist, pts[1] + t2 * dist, pts[1]]))
        return
    u = _chord_params(pts)
    ctrl = _generate(pts, u, t1, t2)
    worst, split = _max_error(pts, ctrl, u)
    if worst < err_sq:
        out.append(_as_segment(ctrl))
        return
    if worst < err_sq * 16:
        for _ in range(8):
            u = _reparam(pts, ctrl, u)
            ctrl = _generate(pts, u, t1, t2)
            worst, split = _max_error(pts, ctrl, u)
            if worst < err_sq:
                out.append(_as_segment(ctrl))
                return
    if depth > 40:
        out.append(_as_segment(ctrl))
        return
    split = max(1, min(len(pts) - 2, split))
    tc = _norm(pts[split - 1] - pts[split + 1])
    if not tc.any():
        tc = _norm(pts[split - 1] - pts[split])
    _fit(pts[: split + 1], t1, tc, err_sq, out, depth + 1)
    _fit(pts[split:], -tc, t2, err_sq, out, depth + 1)


def _end_tangent(pts, forward, reach):
    """Robust tangent: average direction over the first *reach* points."""
    n = len(pts)
    k = min(reach, n - 1)
    if forward:
        return _norm(pts[k] - pts[0])
    return _norm(pts[n - 1 - k] - pts[n - 1])


def fit_open(pts, error, tangent_reach=3):
    """Fit an open run of points; returns a list of cubic segments."""
    pts = _dedupe(pts)
    if len(pts) < 2:
        return []
    out = []
    _fit(pts, _end_tangent(pts, True, tangent_reach), _end_tangent(pts, False, tangent_reach), error * error, out)
    return out


def fit_closed_smooth(pts, error, tangent_reach=3):
    """Fit a closed curve without corners (tangent continuous at the seam)."""
    pts = _dedupe(pts)
    n = len(pts)
    if n < 3:
        return []
    k = min(tangent_reach, n // 2)
    t = _norm(pts[k % n] - pts[-k])
    loop = np.vstack([pts, pts[:1]])
    out = []
    _fit(loop, t, -t, error * error, out)
    return out


def _dedupe(pts):
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return pts
    keep = np.ones(len(pts), dtype=bool)
    keep[1:] = np.any(np.abs(np.diff(pts, axis=0)) > 1e-9, axis=1)
    return pts[keep]
