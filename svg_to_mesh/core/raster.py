"""Tiny triangle rasterizer and PNG encoder (numpy + zlib only).

Used to render a preview of the selected objects for the optional AI
depth suggestions.
"""

import struct
import zlib

import numpy as np


def rasterize(layers, bounds, max_size=512, margin=0.04, background=(150, 150, 150)):
    """Paint triangle layers into an RGB image.

    layers: list of (rgb 0..1, triangles as (n, 3, 2) array) in paint order.
    bounds: (minx, miny, maxx, maxy) of the artwork.
    Returns (image uint8 (h, w, 3), to_px) where to_px maps (x, y) -> pixel.
    """
    minx, miny, maxx, maxy = bounds
    span = max(maxx - minx, maxy - miny, 1e-12)
    pad = span * margin
    minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
    span = max(maxx - minx, maxy - miny)
    scale = max_size / span
    w = max(1, int(round((maxx - minx) * scale)))
    h = max(1, int(round((maxy - miny) * scale)))
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = background

    def to_px(x, y):
        return (x - minx) * scale, (maxy - y) * scale  # image rows go down

    for color, tris in layers:
        rgb = np.clip(np.round(np.asarray(color[:3]) * 255), 0, 255).astype(np.uint8)
        tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 2)
        if not len(tris):
            continue
        px = (tris[..., 0] - minx) * scale
        py = (maxy - tris[..., 1]) * scale
        for (x0, x1, x2), (y0, y1, y2) in zip(px, py):
            lo_x = max(int(np.floor(min(x0, x1, x2))), 0)
            hi_x = min(int(np.ceil(max(x0, x1, x2))), w - 1)
            lo_y = max(int(np.floor(min(y0, y1, y2))), 0)
            hi_y = min(int(np.ceil(max(y0, y1, y2))), h - 1)
            if lo_x > hi_x or lo_y > hi_y:
                continue
            gx, gy = np.meshgrid(np.arange(lo_x, hi_x + 1) + 0.5, np.arange(lo_y, hi_y + 1) + 0.5)
            d0 = (x1 - x0) * (gy - y0) - (y1 - y0) * (gx - x0)
            d1 = (x2 - x1) * (gy - y1) - (y2 - y1) * (gx - x1)
            d2 = (x0 - x2) * (gy - y2) - (y0 - y2) * (gx - x2)
            inside = ((d0 >= 0) & (d1 >= 0) & (d2 >= 0)) | ((d0 <= 0) & (d1 <= 0) & (d2 <= 0))
            img[lo_y:hi_y + 1, lo_x:hi_x + 1][inside] = rgb
    return img, to_px


def encode_png(img):
    """Encode an (h, w, 3) uint8 array as PNG bytes."""
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = img.shape[:2]
    raw = b"".join(b"\x00" + img[row].tobytes() for row in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
