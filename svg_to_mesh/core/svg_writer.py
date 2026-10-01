"""Write VectorShapes back to an SVG file (used to save traced images)."""

from .geometry import is_line


def _fmt(v):
    s = "%.3f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _hex(color):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c * 255)))) for c in color)


def path_data(shape, height):
    """SVG path data; *height* flips the y-up coordinates back to y-down."""
    parts = []

    def p(pt):
        return "%s %s" % (_fmt(pt[0]), _fmt(height - pt[1]))

    for sp in shape.subpaths:
        if not sp.segments:
            continue
        parts.append("M" + p(sp.segments[0][0]))
        for seg in sp.segments:
            if is_line(seg, 1e-6):
                parts.append("L" + p(seg[3]))
            else:
                parts.append("C%s %s %s" % (p(seg[1]), p(seg[2]), p(seg[3])))
        if sp.closed:
            parts.append("Z")
    return "".join(parts)


def shapes_to_svg(shapes, width, height):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s">'
        % (_fmt(width), _fmt(height), _fmt(width), _fmt(height)),
    ]
    for shape in shapes:
        fill = _hex(shape.fill) if shape.fill is not None else "none"
        lines.append(
            '  <path id="%s" fill="%s" fill-rule="%s" d="%s"/>'
            % (shape.name.replace(" ", "_").replace("#", ""), fill, shape.fill_rule, path_data(shape, height))
        )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def write_svg(path, shapes, width, height):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(shapes_to_svg(shapes, width, height))
