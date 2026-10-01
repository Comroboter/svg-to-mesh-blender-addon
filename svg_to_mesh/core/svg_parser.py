"""A small, dependency-free SVG reader.

Supports what logos and icons typically use: ``path`` (all commands incl.
arcs), ``rect`` (rounded), ``circle``, ``ellipse``, ``line``, ``polyline``,
``polygon``, groups, nested ``svg``, ``use``/``symbol``, transforms,
presentation attributes, inline styles, simple ``<style>`` sheets
(tag / .class / #id selectors), ``fill-rule`` and strokes.  Gradients are
reduced to the colour of their first stop.

The result is a list of :class:`~.geometry.VectorShape` in a **y-up**
coordinate system (SVG user units of the root element), in paint order.
"""

import math
import re
import xml.etree.ElementTree as ET

from .geometry import IDENTITY, SubPath, VectorShape, line_segment, mat_mul, transform_shape

KAPPA = 0.5522847498307936

UNIT_TO_PX = {
    "": 1.0,
    "px": 1.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
    "mm": 96.0 / 25.4,
    "cm": 96.0 / 2.54,
    "in": 96.0,
    "em": 16.0,
    "ex": 8.0,
}

NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "lime": "#00ff00",
    "green": "#008000", "blue": "#0000ff", "yellow": "#ffff00", "cyan": "#00ffff",
    "aqua": "#00ffff", "magenta": "#ff00ff", "fuchsia": "#ff00ff", "gray": "#808080",
    "grey": "#808080", "silver": "#c0c0c0", "maroon": "#800000", "olive": "#808000",
    "purple": "#800080", "teal": "#008080", "navy": "#000080", "orange": "#ffa500",
    "pink": "#ffc0cb", "brown": "#a52a2a", "gold": "#ffd700", "darkgray": "#a9a9a9",
    "darkgrey": "#a9a9a9", "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3",
    "darkred": "#8b0000", "darkgreen": "#006400", "darkblue": "#00008b",
    "lightblue": "#add8e6", "skyblue": "#87ceeb", "violet": "#ee82ee",
    "indigo": "#4b0082", "coral": "#ff7f50", "salmon": "#fa8072", "tomato": "#ff6347",
    "crimson": "#dc143c", "khaki": "#f0e68c", "beige": "#f5f5dc", "ivory": "#fffff0",
    "tan": "#d2b48c", "chocolate": "#d2691e", "orchid": "#da70d6", "plum": "#dda0dd",
    "turquoise": "#40e0d0", "steelblue": "#4682b4", "royalblue": "#4169e1",
    "dodgerblue": "#1e90ff", "deepskyblue": "#00bfff", "slategray": "#708090",
    "dimgray": "#696969", "whitesmoke": "#f5f5f5", "gainsboro": "#dcdcdc",
    "firebrick": "#b22222", "forestgreen": "#228b22", "seagreen": "#2e8b57",
    "limegreen": "#32cd32", "darkorange": "#ff8c00", "orangered": "#ff4500",
    "hotpink": "#ff69b4", "deeppink": "#ff1493", "midnightblue": "#191970",
}

INHERITED = (
    "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-opacity", "visibility", "color",
)
STYLE_PROPS = INHERITED + ("display", "opacity", "clip-path", "filter")

SKIP_TAGS = {
    "defs", "clipPath", "mask", "pattern", "marker", "symbol", "style", "title",
    "desc", "metadata", "linearGradient", "radialGradient", "filter", "script",
    "foreignObject", "image", "namedview",
}


# Limits that protect against malicious or broken files (e.g. "<use> bombs"
# where a few references expand to billions of shapes).
MAX_ELEMENTS = 200000  # elements visited, counting every <use> expansion
MAX_DEPTH = 400  # nesting depth of groups / <use> chains


class SvgError(ValueError):
    """The SVG is invalid or exceeds the safety limits."""


class SvgDocument:
    def __init__(self):
        self.shapes = []
        self.width_px = None
        self.height_px = None
        self.mm_per_unit = 25.4 / 96.0  # millimetres per root user unit
        self.warnings = []


# --------------------------------------------------------------------------
# Small parsers
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_length(value, default=0.0, percent_of=None):
    if value is None:
        return default
    value = value.strip()
    m = re.match(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-z%]*)$", value, re.I)
    if not m:
        return default
    num = float(m.group(1))
    if not math.isfinite(num):
        return default
    unit = m.group(2).lower()
    if unit == "%":
        return num / 100.0 * percent_of if percent_of is not None else default
    return num * UNIT_TO_PX.get(unit, 1.0)


def parse_numbers(text):
    return [v for v in (float(x) for x in _NUM_RE.findall(text or "")) if math.isfinite(v)]


def parse_color(value, current_color=None, gradients=None):
    """Return an (r, g, b) tuple in 0..1, or None for 'none'."""
    if value is None:
        return None
    v = value.strip()
    low = v.lower()
    if low in ("none", "transparent", ""):
        return None
    if low == "currentcolor":
        return current_color if current_color is not None else (0.0, 0.0, 0.0)
    if low.startswith("url("):
        m = re.match(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)", v)
        if m and gradients is not None and m.group(1) in gradients:
            return gradients[m.group(1)]
        fallback = v[v.find(")") + 1:].strip()
        if fallback:
            return parse_color(fallback, current_color, gradients)
        return (0.5, 0.5, 0.5)
    low = NAMED_COLORS.get(low, low)
    if low.startswith("#"):
        h = low[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h[:3])
        if len(h) >= 6:
            try:
                return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            except ValueError:
                return (0.0, 0.0, 0.0)
        return (0.0, 0.0, 0.0)
    m = re.match(r"rgba?\(([^)]*)\)", low)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        rgb = []
        for p in parts[:3]:
            if p.endswith("%"):
                rgb.append(float(p[:-1]) / 100.0)
            else:
                rgb.append(float(p) / 255.0)
        while len(rgb) < 3:
            rgb.append(0.0)
        return tuple(max(0.0, min(1.0, c)) for c in rgb)
    m = re.match(r"hsla?\(([^)]*)\)", low)
    if m:
        parts = [p.strip().rstrip("%").replace("deg", "") for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        h, s, l_ = float(parts[0]) / 360.0, float(parts[1]) / 100.0, float(parts[2]) / 100.0
        import colorsys

        return colorsys.hls_to_rgb(h % 1.0, l_, s)
    return (0.0, 0.0, 0.0)


def parse_transform(text):
    m_total = IDENTITY
    if not text:
        return m_total
    for name, args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", text):
        a = parse_numbers(args)
        name = name.lower()
        if name == "matrix" and len(a) == 6:
            m = tuple(a)
        elif name == "translate" and a:
            m = (1, 0, 0, 1, a[0], a[1] if len(a) > 1 else 0.0)
        elif name == "scale" and a:
            m = (a[0], 0, 0, a[1] if len(a) > 1 else a[0], 0, 0)
        elif name == "rotate" and a:
            r = math.radians(a[0])
            cs, sn = math.cos(r), math.sin(r)
            m = (cs, sn, -sn, cs, 0, 0)
            if len(a) >= 3:
                cx, cy = a[1], a[2]
                m = mat_mul((1, 0, 0, 1, cx, cy), mat_mul(m, (1, 0, 0, 1, -cx, -cy)))
        elif name == "skewx" and a:
            m = (1, 0, math.tan(math.radians(a[0])), 1, 0, 0)
        elif name == "skewy" and a:
            m = (1, math.tan(math.radians(a[0])), 0, 1, 0, 0)
        else:
            continue
        m_total = mat_mul(m_total, m)
    return m_total


def parse_style_attr(text):
    out = {}
    if not text:
        return out
    for decl in text.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.replace("!important", "").strip()
    return out


# --------------------------------------------------------------------------
# CSS (very small subset)
# --------------------------------------------------------------------------


class StyleSheet:
    def __init__(self):
        self.rules = []  # (specificity, order, (tag, classes, id), decls)

    def add(self, css):
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        css = re.sub(r"@[^{;]+;", "", css)  # @import etc.
        for sel_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            decls = parse_style_attr(body)
            for sel in sel_text.split(","):
                sel = sel.strip()
                if not sel or " " in sel or ">" in sel or ":" in sel or "[" in sel:
                    # descendant / pseudo selectors: use last compound only
                    sel = re.split(r"[\s>+~]+", sel)[-1] if sel else sel
                    sel = sel.split(":")[0]
                    if not sel:
                        continue
                parsed = self._parse_selector(sel)
                if parsed is None:
                    continue
                tag, classes, ident = parsed
                spec = (100 if ident else 0) + 10 * len(classes) + (1 if tag and tag != "*" else 0)
                self.rules.append((spec, len(self.rules), parsed, decls))
        self.rules.sort(key=lambda r: (r[0], r[1]))

    @staticmethod
    def _parse_selector(sel):
        m = re.match(r"^([a-zA-Z*][\w-]*)?((?:[.#][\w-]+)*)$", sel)
        if not m:
            return None
        tag = m.group(1)
        classes = re.findall(r"\.([\w-]+)", m.group(2))
        ids = re.findall(r"#([\w-]+)", m.group(2))
        return tag, classes, ids[0] if ids else None

    def match(self, tag, classes, ident):
        out = {}
        for _spec, _order, (stag, sclasses, sid), decls in self.rules:
            if stag and stag != "*" and stag != tag:
                continue
            if sid and sid != ident:
                continue
            if any(c not in classes for c in sclasses):
                continue
            out.update(decls)
        return out


# --------------------------------------------------------------------------
# Path data
# --------------------------------------------------------------------------


class _PathScanner:
    def __init__(self, d):
        self.d = d
        self.i = 0
        self.n = len(d)

    def skip(self):
        while self.i < self.n and self.d[self.i] in " \t\r\n,":
            self.i += 1

    def at_end(self):
        self.skip()
        return self.i >= self.n

    def peek_command(self):
        self.skip()
        if self.i < self.n and self.d[self.i].isalpha() and self.d[self.i] not in "eE":
            return self.d[self.i]
        return None

    def has_number(self):
        self.skip()
        return self.i < self.n and (self.d[self.i].isdigit() or self.d[self.i] in "+-.")

    def number(self):
        self.skip()
        m = _NUM_RE.match(self.d, self.i)
        if not m:
            raise ValueError("number expected at %d" % self.i)
        self.i = m.end()
        v = float(m.group(0))
        if not math.isfinite(v):
            raise ValueError("number out of range at %d" % self.i)
        return v

    def flag(self):
        self.skip()
        if self.i < self.n and self.d[self.i] in "01":
            self.i += 1
            return self.d[self.i - 1] == "1"
        raise ValueError("flag expected at %d" % self.i)


def arc_to_cubics(p0, rx, ry, phi_deg, large, sweep, p1):
    """SVG elliptical arc (endpoint form) to cubic segments."""
    if p0 == p1:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [line_segment(p0, p1)]
    phi = math.radians(phi_deg % 360.0)
    cp, sp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (p0[0] - p1[0]) / 2.0, (p0[1] - p1[1]) / 2.0
    x1p = cp * dx2 + sp * dy2
    y1p = -sp * dx2 + cp * dy2
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (p0[0] + p1[0]) / 2.0
    cy = sp * cxp + cp * cyp + (p0[1] + p1[1]) / 2.0

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(1, int(math.ceil(abs(dt) / (math.pi / 2) - 1e-9)))
    step = dt / n
    k = 4.0 / 3.0 * math.tan(step / 4.0)

    def pt(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (cp * x - sp * y + cx, sp * x + cp * y + cy)

    def deriv(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return (cp * x - sp * y, sp * x + cp * y)

    segs = []
    t = t1
    start = p0
    for i in range(n):
        t_next = t + step
        end = p1 if i == n - 1 else pt(t_next)
        d0 = deriv(t)
        d1 = deriv(t_next)
        c1 = (start[0] + k * d0[0], start[1] + k * d0[1])
        c2 = (end[0] - k * d1[0], end[1] - k * d1[1])
        segs.append((start, c1, c2, end))
        start = end
        t = t_next
    return segs


def parse_path_data(d):
    """Parse SVG path data into a list of SubPath (absolute coordinates)."""
    sc = _PathScanner(d or "")
    subpaths = []
    cur = None
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    last_c = None  # last cubic control point (for S)
    last_q = None  # last quadratic control point (for T)
    cmd = None

    def ensure():
        nonlocal cur
        if cur is None:
            cur = SubPath([], False)
            subpaths.append(cur)
        return cur

    try:
        while not sc.at_end():
            c = sc.peek_command()
            if c is not None:
                sc.i += 1
                cmd = c
            elif cmd is None:
                break
            elif cmd in "Mm":
                cmd = "L" if cmd == "M" else "l"  # implicit lineto
            elif cmd in "Zz":
                break
            rel = cmd.islower()
            C = cmd.upper()
            ox, oy = pos if rel else (0.0, 0.0)
            if C == "M":
                x, y = sc.number() + ox, sc.number() + oy
                cur = SubPath([], False)
                subpaths.append(cur)
                pos = start = (x, y)
                last_c = last_q = None
                continue
            if C == "Z":
                sp = ensure()
                if pos != start:
                    sp.segments.append(line_segment(pos, start))
                sp.closed = True
                pos = start
                cur = None
                last_c = last_q = None
                continue
            if cur is None:
                # drawing after Z without M continues from subpath start
                cur = SubPath([], False)
                subpaths.append(cur)
                start = pos
            if C == "L":
                p = (sc.number() + ox, sc.number() + oy)
                cur.segments.append(line_segment(pos, p))
                pos = p
                last_c = last_q = None
            elif C == "H":
                p = (sc.number() + ox, pos[1])
                cur.segments.append(line_segment(pos, p))
                pos = p
                last_c = last_q = None
            elif C == "V":
                p = (pos[0], sc.number() + oy)
                cur.segments.append(line_segment(pos, p))
                pos = p
                last_c = last_q = None
            elif C == "C":
                c1 = (sc.number() + ox, sc.number() + oy)
                c2 = (sc.number() + ox, sc.number() + oy)
                p = (sc.number() + ox, sc.number() + oy)
                cur.segments.append((pos, c1, c2, p))
                pos, last_c, last_q = p, c2, None
            elif C == "S":
                c1 = (2 * pos[0] - last_c[0], 2 * pos[1] - last_c[1]) if last_c else pos
                c2 = (sc.number() + ox, sc.number() + oy)
                p = (sc.number() + ox, sc.number() + oy)
                cur.segments.append((pos, c1, c2, p))
                pos, last_c, last_q = p, c2, None
            elif C in "QT":
                if C == "Q":
                    q = (sc.number() + ox, sc.number() + oy)
                else:
                    q = (2 * pos[0] - last_q[0], 2 * pos[1] - last_q[1]) if last_q else pos
                p = (sc.number() + ox, sc.number() + oy)
                c1 = (pos[0] + 2.0 / 3.0 * (q[0] - pos[0]), pos[1] + 2.0 / 3.0 * (q[1] - pos[1]))
                c2 = (p[0] + 2.0 / 3.0 * (q[0] - p[0]), p[1] + 2.0 / 3.0 * (q[1] - p[1]))
                cur.segments.append((pos, c1, c2, p))
                pos, last_q, last_c = p, q, None
            elif C == "A":
                rx, ry, rot = sc.number(), sc.number(), sc.number()
                large, sweep = sc.flag(), sc.flag()
                p = (sc.number() + ox, sc.number() + oy)
                cur.segments.extend(arc_to_cubics(pos, rx, ry, rot, large, sweep, p))
                pos = p
                last_c = last_q = None
            else:
                break
    except (ValueError, TypeError, IndexError):
        pass  # per spec: render up to the first error
    return [sp for sp in subpaths if sp.segments]


# --------------------------------------------------------------------------
# Basic shapes
# --------------------------------------------------------------------------


def _poly_subpath(points, closed):
    segs = [line_segment(points[i], points[i + 1]) for i in range(len(points) - 1)]
    if closed and len(points) > 2 and points[0] != points[-1]:
        segs.append(line_segment(points[-1], points[0]))
    return SubPath(segs, closed) if segs else None


def ellipse_subpath(cx, cy, rx, ry):
    kx, ky = rx * KAPPA, ry * KAPPA
    p = [(cx + rx, cy), (cx, cy + ry), (cx - rx, cy), (cx, cy - ry)]
    segs = [
        (p[0], (cx + rx, cy + ky), (cx + kx, cy + ry), p[1]),
        (p[1], (cx - kx, cy + ry), (cx - rx, cy + ky), p[2]),
        (p[2], (cx - rx, cy - ky), (cx - kx, cy - ry), p[3]),
        (p[3], (cx + kx, cy - ry), (cx + rx, cy - ky), p[0]),
    ]
    return SubPath(segs, True)


def rect_subpath(x, y, w, h, rx, ry):
    if rx <= 0 and ry <= 0:
        return _poly_subpath([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)
    if rx <= 0:
        rx = ry
    if ry <= 0:
        ry = rx
    rx, ry = min(rx, w / 2.0), min(ry, h / 2.0)
    kx, ky = rx * KAPPA, ry * KAPPA
    segs = [
        line_segment((x + rx, y), (x + w - rx, y)),
        ((x + w - rx, y), (x + w - rx + kx, y), (x + w, y + ry - ky), (x + w, y + ry)),
        line_segment((x + w, y + ry), (x + w, y + h - ry)),
        ((x + w, y + h - ry), (x + w, y + h - ry + ky), (x + w - rx + kx, y + h), (x + w - rx, y + h)),
        line_segment((x + w - rx, y + h), (x + rx, y + h)),
        ((x + rx, y + h), (x + rx - kx, y + h), (x, y + h - ry + ky), (x, y + h - ry)),
        line_segment((x, y + h - ry), (x, y + ry)),
        ((x, y + ry), (x, y + ry - ky), (x + rx - kx, y), (x + rx, y)),
    ]
    segs = [s for s in segs if s[0] != s[3]]
    return SubPath(segs, True)


# --------------------------------------------------------------------------
# Document walker
# --------------------------------------------------------------------------


class _Parser:
    def __init__(self, root):
        self.root = root
        self.doc = SvgDocument()
        self.ids = {}
        self.sheet = StyleSheet()
        self.gradients = {}
        self.gradient_opacity = {}
        self.use_depth = 0
        self.visited = 0
        self.depth = 0
        for el in root.iter():
            ident = el.get("id")
            if ident:
                self.ids[ident] = el
            if _local(el.tag) == "style" and el.text:
                self.sheet.add(el.text)
        self._collect_gradients()

    # -- gradients -> flat colour of first stop --------------------------
    def _collect_gradients(self):
        """Reduce gradients to one color: the average of their stops weighted by
        stop opacity, plus the average opacity (a fade to transparent counts as
        half transparent, not as its first color)."""
        def stops(el, depth=0):
            found = []
            for st in el:
                if _local(st.tag) == "stop":
                    style = parse_style_attr(st.get("style"))
                    col = parse_color(style.get("stop-color", st.get("stop-color", "black")))
                    op = _float(style.get("stop-opacity", st.get("stop-opacity")), 1.0)
                    if col is not None:
                        found.append((col, max(0.0, min(1.0, op))))
            if found:
                return found
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
            if href and href.startswith("#") and depth < 8 and href[1:] in self.ids:
                return stops(self.ids[href[1:]], depth + 1)
            return []

        for ident, el in self.ids.items():
            if _local(el.tag) in ("linearGradient", "radialGradient"):
                found = stops(el)
                if not found:
                    continue
                total = sum(op for _c, op in found)
                if total > 1e-6:
                    col = tuple(sum(c[i] * op for c, op in found) / total for i in range(3))
                else:
                    col = tuple(sum(c[i] for c, _op in found) / len(found) for i in range(3))
                self.gradients[ident] = col
                self.gradient_opacity[ident] = total / len(found)

    # -- style resolution -----------------------------------------------
    def element_style(self, el, parent_style):
        style = {k: v for k, v in parent_style.items() if k in INHERITED}
        for k in STYLE_PROPS:
            if el.get(k) is not None:
                style[k] = el.get(k)
        classes = (el.get("class") or "").split()
        style.update({k: v for k, v in self.sheet.match(_local(el.tag), classes, el.get("id")).items()})
        style.update(parse_style_attr(el.get("style")))
        for k, v in list(style.items()):
            if v == "inherit":
                if k in parent_style:
                    style[k] = parent_style[k]
                else:
                    del style[k]
        opacity = _float(style.get("opacity"), 1.0)
        style["_opacity"] = parent_style.get("_opacity", 1.0) * opacity
        style["_clips"] = parent_style.get("_clips", ())
        style["_blur"] = parent_style.get("_blur", 0.0)
        return style

    def filter_blur(self, ref):
        """Largest Gaussian blur radius (stdDeviation, user units) of filter *ref*."""
        el = self.ids.get(ref)
        if el is None or _local(el.tag) != "filter":
            return 0.0
        blur = 0.0
        for child in el.iter():
            if _local(child.tag) == "feGaussianBlur":
                blur = max([blur] + [abs(v) for v in parse_numbers(child.get("stdDeviation"))])
        return blur

    # -- clip paths -------------------------------------------------------
    def clip_group(self, ref, m):
        """Shapes (in output coordinates) of the clipPath *ref*, or None."""
        el = self.ids.get(ref)
        if el is None or _local(el.tag) != "clipPath":
            return None
        if (el.get("clipPathUnits") or "userSpaceOnUse").strip() != "userSpaceOnUse":
            return None  # objectBoundingBox clips are rare in logos: ignore them
        cm = mat_mul(m, parse_transform(el.get("transform")))
        group = []
        for child in el:
            tag = _local(child.tag)
            target, cm2 = child, mat_mul(cm, parse_transform(child.get("transform")))
            if tag == "use":
                href = child.get("{http://www.w3.org/1999/xlink}href") or child.get("href")
                target = self.ids.get(href[1:]) if href and href.startswith("#") else None
                if target is None:
                    continue
                cm2 = mat_mul(cm2, (1, 0, 0, 1, parse_length(child.get("x")), parse_length(child.get("y"))))
                cm2 = mat_mul(cm2, parse_transform(target.get("transform")))
                tag = _local(target.tag)
            subpaths = self.geometry(tag, target)
            if not subpaths:
                continue
            style = parse_style_attr(child.get("style"))
            rule = style.get("clip-rule", child.get("clip-rule", el.get("clip-rule", "nonzero")))
            shape = VectorShape(subpaths=subpaths, fill=(0.0, 0.0, 0.0),
                                fill_rule="evenodd" if str(rule).strip() == "evenodd" else "nonzero")
            group.append(transform_shape(shape, cm2))
        return group

    # -- walking ---------------------------------------------------------
    def walk(self, el, ctm, parent_style):
        self.visited += 1
        if self.visited > MAX_ELEMENTS:
            raise SvgError("SVG is too complex (more than %d elements after expanding <use>)" % MAX_ELEMENTS)
        if self.depth >= MAX_DEPTH:
            raise SvgError("SVG is nested too deeply (more than %d levels)" % MAX_DEPTH)
        self.depth += 1
        try:
            self._walk(el, ctm, parent_style)
        finally:
            self.depth -= 1

    def _walk(self, el, ctm, parent_style):
        tag = _local(el.tag)
        if tag in SKIP_TAGS:
            return
        style = self.element_style(el, parent_style)
        if style.get("display", "").strip() == "none":
            return
        m = mat_mul(ctm, parse_transform(el.get("transform")))
        filter_ref = _url_id(style.get("filter"))
        if filter_ref:
            scale = abs(m[0] * m[3] - m[1] * m[2]) ** 0.5
            style["_blur"] = max(style["_blur"], self.filter_blur(filter_ref) * scale)
        clip_ref = _url_id(style.get("clip-path"))
        if clip_ref:
            group = self.clip_group(clip_ref, m)
            if group is not None:
                style["_clips"] = style["_clips"] + (group,)

        if tag == "svg" and el is not self.root:
            x = parse_length(el.get("x"))
            y = parse_length(el.get("y"))
            m = mat_mul(m, (1, 0, 0, 1, x, y))
            vb = parse_numbers(el.get("viewBox"))
            w = parse_length(el.get("width"), None)
            h = parse_length(el.get("height"), None)
            if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
                w = w if w else vb[2]
                h = h if h else vb[3]
                s = min(w / vb[2], h / vb[3])
                m = mat_mul(m, (s, 0, 0, s, -vb[0] * s, -vb[1] * s))
        if tag in ("svg", "g", "a", "switch"):
            for child in el:
                self.walk(child, m, style)
            return
        if tag == "use":
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
            if not href or not href.startswith("#") or self.use_depth > 16:
                return
            target = self.ids.get(href[1:])
            if target is None:
                return
            m = mat_mul(m, (1, 0, 0, 1, parse_length(el.get("x")), parse_length(el.get("y"))))
            self.use_depth += 1
            if _local(target.tag) == "symbol":
                vb = parse_numbers(target.get("viewBox"))
                if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
                    w = parse_length(el.get("width"), vb[2])
                    h = parse_length(el.get("height"), vb[3])
                    s = min(w / vb[2], h / vb[3])
                    m = mat_mul(m, (s, 0, 0, s, -vb[0] * s, -vb[1] * s))
                sstyle = self.element_style(target, style)
                for child in target:
                    self.walk(child, m, sstyle)
            else:
                self.walk(target, m, style)
            self.use_depth -= 1
            return
        if tag == "text":
            self.doc.warnings.append("SVG <text> is not supported - convert text to paths first.")
            return
        subpaths = self.geometry(tag, el)
        if not subpaths:
            return
        self.add_shape(el, tag, subpaths, style, m)

    def geometry(self, tag, el):
        g = lambda k: parse_length(el.get(k))  # noqa: E731
        if tag == "path":
            return parse_path_data(el.get("d"))
        if tag == "rect":
            w, h = g("width"), g("height")
            if w <= 0 or h <= 0:
                return []
            rx = parse_length(el.get("rx"), -1)
            ry = parse_length(el.get("ry"), -1)
            return [rect_subpath(g("x"), g("y"), w, h, rx, ry)]
        if tag == "circle":
            r = g("r")
            return [ellipse_subpath(g("cx"), g("cy"), r, r)] if r > 0 else []
        if tag == "ellipse":
            rx, ry = g("rx"), g("ry")
            if rx > 0 and ry <= 0 and el.get("ry") in (None, "auto"):
                ry = rx
            if ry > 0 and rx <= 0 and el.get("rx") in (None, "auto"):
                rx = ry
            return [ellipse_subpath(g("cx"), g("cy"), rx, ry)] if rx > 0 and ry > 0 else []
        if tag == "line":
            sp = _poly_subpath([(g("x1"), g("y1")), (g("x2"), g("y2"))], False)
            return [sp] if sp else []
        if tag in ("polyline", "polygon"):
            nums = parse_numbers(el.get("points"))
            pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
            if len(pts) < 2:
                return []
            sp = _poly_subpath(pts, tag == "polygon")
            return [sp] if sp else []
        return []

    def add_shape(self, el, tag, subpaths, style, m):
        if style.get("visibility", "visible").strip() in ("hidden", "collapse"):
            return
        current = parse_color(style.get("color", "black"))
        fill = parse_color(style.get("fill", "black"), current, self.gradients)
        if tag == "line":
            fill = None
        fill_op = _float(style.get("fill-opacity"), 1.0) * style["_opacity"]
        fill_op *= self.gradient_opacity.get(_url_id(style.get("fill")), 1.0)
        if fill is not None and fill_op <= 0.001:
            fill = None
        stroke = parse_color(style.get("stroke", "none"), current, self.gradients)
        stroke_op = _float(style.get("stroke-opacity"), 1.0) * style["_opacity"]
        stroke_op *= self.gradient_opacity.get(_url_id(style.get("stroke")), 1.0)
        if stroke is not None and stroke_op <= 0.001:
            stroke = None
        if fill is None and stroke is None:
            return
        shape = VectorShape(
            subpaths=subpaths,
            fill=fill,
            fill_rule="evenodd" if style.get("fill-rule", "nonzero").strip() == "evenodd" else "nonzero",
            stroke=stroke,
            stroke_width=parse_length(style.get("stroke-width"), 1.0),
            linecap=style.get("stroke-linecap", "butt").strip(),
            linejoin=style.get("stroke-linejoin", "miter").strip(),
            miterlimit=_float(style.get("stroke-miterlimit"), 4.0),
            name=el.get("id") or el.get("{http://www.inkscape.org/namespaces/inkscape}label") or tag,
            fill_opacity=fill_op,
            stroke_opacity=stroke_op,
            blur=style.get("_blur", 0.0),
        )
        transform_shape(shape, m)
        shape.clips = style.get("_clips", ())
        self.doc.shapes.append(shape)

    # -- root ------------------------------------------------------------
    def run(self):
        root = self.root
        vb = parse_numbers(root.get("viewBox"))
        w = parse_length(root.get("width"), None, percent_of=None)
        h = parse_length(root.get("height"), None, percent_of=None)
        m = IDENTITY
        if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
            if not w and not h:
                w, h = vb[2], vb[3]
            elif not w:
                w = h * vb[2] / vb[3]
            elif not h:
                h = w * vb[3] / vb[2]
            s = min(w / vb[2], h / vb[3])
            tx = -vb[0] * s + (w - vb[2] * s) / 2.0
            ty = -vb[1] * s + (h - vb[3] * s) / 2.0
            m = (s, 0, 0, s, tx, ty)
        self.doc.width_px = w
        self.doc.height_px = h
        # Everything is returned in px; flip y so that "up" is +Y.
        flip_h = h if h else 0.0
        m = mat_mul((1, 0, 0, -1, 0, flip_h), m)
        self.walk(root, m, {"_opacity": 1.0})
        self.doc.mm_per_unit = 25.4 / 96.0
        return self.doc


def _url_id(value):
    """'url(#id)' -> 'id' (None otherwise)."""
    if not value:
        return None
    m = re.match(r"\s*url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)", str(value))
    return m.group(1) if m else None


def _float(v, default):
    try:
        return float(str(v).strip().rstrip("%")) / (100.0 if str(v).strip().endswith("%") else 1.0)
    except (TypeError, ValueError):
        return default


def parse_svg(source):
    """Parse an SVG file path, bytes or string into an :class:`SvgDocument`."""
    if isinstance(source, bytes):
        root = ET.fromstring(source)
    elif isinstance(source, str) and source.lstrip().startswith("<"):
        root = ET.fromstring(source)
    else:
        root = ET.parse(source).getroot()
    return _Parser(root).run()
