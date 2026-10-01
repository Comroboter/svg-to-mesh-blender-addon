"""Least-squares cubic Bézier fitting of point runs.

Implementation of Philip J. Schneider's algorithm ("An Algorithm for
Automatically Fitting Digitized Curves", Graphics Gems, 1990).
"""

import math


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _mul(a, s):
    return (a[0] * s, a[1] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _norm(a):
    length = math.hypot(a[0], a[1])
    return (a[0] / length, a[1] / length) if length > 1e-12 else (0.0, 0.0)


def _bezier(ctrl, t):
    mt = 1.0 - t
    a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
    return (
        a * ctrl[0][0] + b * ctrl[1][0] + c * ctrl[2][0] + d * ctrl[3][0],
        a * ctrl[0][1] + b * ctrl[1][1] + c * ctrl[2][1] + d * ctrl[3][1],
    )


def _bezier_d1(ctrl, t):
    mt = 1.0 - t
    q = [_mul(_sub(ctrl[i + 1], ctrl[i]), 3.0) for i in range(3)]
    a, b, c = mt * mt, 2 * mt * t, t * t
    return (a * q[0][0] + b * q[1][0] + c * q[2][0], a * q[0][1] + b * q[1][1] + c * q[2][1])


def _bezier_d2(ctrl, t):
    q = [_mul(_sub(ctrl[i + 1], ctrl[i]), 3.0) for i in range(3)]
    r = [_mul(_sub(q[i + 1], q[i]), 2.0) for i in range(2)]
    return ((1 - t) * r[0][0] + t * r[1][0], (1 - t) * r[0][1] + t * r[1][1])


def _chord_params(pts):
    u = [0.0]
    for i in range(1, len(pts)):
        u.append(u[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    total = u[-1] or 1.0
    return [x / total for x in u]


def _generate(pts, u, t1, t2):
    first, last = pts[0], pts[-1]
    c00 = c01 = c11 = x0 = x1 = 0.0
    for p, t in zip(pts, u):
        mt = 1 - t
        b0, b1, b2, b3 = mt * mt * mt, 3 * t * mt * mt, 3 * t * t * mt, t * t * t
        a1 = _mul(t1, b1)
        a2 = _mul(t2, b2)
        c00 += _dot(a1, a1)
        c01 += _dot(a1, a2)
        c11 += _dot(a2, a2)
        tmp = _sub(p, _add(_mul(first, b0 + b1), _mul(last, b2 + b3)))
        x0 += _dot(a1, tmp)
        x1 += _dot(a2, tmp)
    det = c00 * c11 - c01 * c01
    seg_len = math.hypot(last[0] - first[0], last[1] - first[1])
    eps = 1e-6 * seg_len
    if abs(det) > 1e-12:
        alpha_l = (x0 * c11 - c01 * x1) / det
        alpha_r = (c00 * x1 - x0 * c01) / det
    else:
        alpha_l = alpha_r = 0.0
    if alpha_l < eps or alpha_r < eps:
        alpha_l = alpha_r = seg_len / 3.0
    return (first, _add(first, _mul(t1, alpha_l)), _add(last, _mul(t2, alpha_r)), last)


def _max_error(pts, ctrl, u):
    worst, idx = 0.0, len(pts) // 2
    for i in range(1, len(pts) - 1):
        q = _bezier(ctrl, u[i])
        d = (q[0] - pts[i][0]) ** 2 + (q[1] - pts[i][1]) ** 2
        if d >= worst:
            worst, idx = d, i
    return worst, idx


def _reparam(pts, ctrl, u):
    out = []
    for p, t in zip(pts, u):
        d = _sub(_bezier(ctrl, t), p)
        d1 = _bezier_d1(ctrl, t)
        d2 = _bezier_d2(ctrl, t)
        num = _dot(d, d1)
        den = _dot(d1, d1) + _dot(d, d2)
        nt = t - num / den if abs(den) > 1e-12 else t
        out.append(min(1.0, max(0.0, nt)))
    return out


def _fit(pts, t1, t2, err_sq, out, depth=0):
    if len(pts) == 2:
        dist = math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]) / 3.0
        out.append((pts[0], _add(pts[0], _mul(t1, dist)), _add(pts[1], _mul(t2, dist)), pts[1]))
        return
    u = _chord_params(pts)
    ctrl = _generate(pts, u, t1, t2)
    worst, split = _max_error(pts, ctrl, u)
    if worst < err_sq:
        out.append(ctrl)
        return
    if worst < err_sq * 16:
        for _ in range(8):
            u = _reparam(pts, ctrl, u)
            ctrl = _generate(pts, u, t1, t2)
            worst, split = _max_error(pts, ctrl, u)
            if worst < err_sq:
                out.append(ctrl)
                return
    if depth > 40:
        out.append(ctrl)
        return
    split = max(1, min(len(pts) - 2, split))
    tc = _norm(_sub(pts[split - 1], pts[split + 1]))
    if tc == (0.0, 0.0):
        tc = _norm(_sub(pts[split - 1], pts[split]))
    _fit(pts[: split + 1], t1, tc, err_sq, out, depth + 1)
    _fit(pts[split:], (-tc[0], -tc[1]), t2, err_sq, out, depth + 1)


def _end_tangent(pts, forward, reach):
    """Robust tangent: average direction over the first *reach* points."""
    n = len(pts)
    k = min(reach, n - 1)
    if forward:
        return _norm(_sub(pts[k], pts[0]))
    return _norm(_sub(pts[n - 1 - k], pts[n - 1]))


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
    t = _norm(_sub(pts[k % n], pts[-k]))
    loop = pts + [pts[0]]
    out = []
    _fit(loop, t, (-t[0], -t[1]), error * error, out)
    return out


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    return out
