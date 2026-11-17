"""Build the AgenticMind logo assets from one geometric definition.

The mark is the infinity ribbon from the brand artwork: two rounded triangles whose inner edges cross,
drawn as one ribbon with the violet → magenta → orange gradient (no glow), with an over/under at the
crossing. Coordinates are in the original 1536×1024 artwork frame.

    uv run python scripts/build_logo.py

Outputs: apps/web/public/{logo.svg, favicon.svg, favicon.ico, favicon-32.png, logo-192.png, logo-512.png}
and docs/assets/logo-mark.svg.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "apps" / "web" / "public"

# Ribbon centre-line vertices (closed figure-of-eight), measured from the artwork.
BL = (268, 690)  # left loop, bottom-left corner
T1 = (545, 335)  # left loop apex
B2 = (1016, 690)  # right loop, bottom-left corner (end of the left loop's inner diagonal)
BR = (1268, 690)  # right loop, bottom-right corner
T2 = (991, 335)  # right loop apex
B1 = (520, 690)  # left loop, bottom-right corner (end of the right loop's inner diagonal)
PATH = [BL, T1, B2, BR, T2, B1]

STROKE = 128  # ribbon width
RADIUS = 70  # corner rounding along the centre line
GAP = 14  # visual gap where the ribbon passes under itself
STOPS = [(0.0, "#6b1bfd"), (0.22, "#8a1dfc"), (0.5, "#fd22c4"), (0.72, "#fd2079"), (0.88, "#fd3536"), (1.0, "#fd4216")]
X0, X1 = BL[0] - STROKE / 2, BR[0] + STROKE / 2

PAD = 10
VB = (X0 - PAD, T1[1] - STROKE / 2 - PAD, (X1 - X0) + 2 * PAD, (BL[1] - T1[1]) + STROKE + 2 * PAD)


def _toward(a, b, dist):
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    return (a[0] + dx / n * dist, a[1] + dy / n * dist)


def rounded_points(points, radius, steps=12):
    """Closed polyline with quadratic-rounded corners, sampled as points (for raster drawing)."""
    out = []
    n = len(points)
    for i in range(n):
        prev, cur, nxt = points[i - 1], points[i], points[(i + 1) % n]
        a = _toward(cur, prev, radius)
        b = _toward(cur, nxt, radius)
        for s in range(steps + 1):
            t = s / steps
            out.append(
                (
                    (1 - t) ** 2 * a[0] + 2 * (1 - t) * t * cur[0] + t**2 * b[0],
                    (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * cur[1] + t**2 * b[1],
                )
            )
    out.append(out[0])
    return out


def rounded_svg_path(points, radius):
    n = len(points)
    parts = []
    for i in range(n):
        prev, cur, nxt = points[i - 1], points[i], points[(i + 1) % n]
        a = _toward(cur, prev, radius)
        b = _toward(cur, nxt, radius)
        parts.append(f"{'M' if i == 0 else 'L'}{a[0]:.1f} {a[1]:.1f} Q{cur[0]} {cur[1]} {b[0]:.1f} {b[1]:.1f}")
    return " ".join(parts) + " Z"


def crossing_segment():
    """Short piece of the T1→B2 diagonal around the crossing, redrawn on top for the over/under."""
    cx = (T1[0] + B2[0]) / 2
    cy = (T1[1] + B2[1]) / 2
    half = STROKE * 1.1
    a = _toward((cx, cy), T1, half)
    b = _toward((cx, cy), B2, half)
    return a, b


def svg(title: bool = True) -> str:
    stops = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in STOPS)
    (ax, ay), (bx, by) = crossing_segment()
    vb = " ".join(f"{v:.0f}" for v in VB)
    t = "<title>AgenticMind</title>" if title else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" role="img" aria-label="AgenticMind">{t}'
        f'<defs><linearGradient id="am-g" gradientUnits="userSpaceOnUse" x1="{X0:.0f}" y1="0" x2="{X1:.0f}" y2="0">'
        f"{stops}</linearGradient>"
        f'<mask id="am-under" maskUnits="userSpaceOnUse" x="{VB[0]:.0f}" y="{VB[1]:.0f}" '
        f'width="{VB[2]:.0f}" height="{VB[3]:.0f}"><rect x="{VB[0]:.0f}" y="{VB[1]:.0f}" '
        f'width="{VB[2]:.0f}" height="{VB[3]:.0f}" fill="#fff"/>'
        f'<path d="M{ax:.1f} {ay:.1f} L{bx:.1f} {by:.1f}" stroke="#000" stroke-width="{STROKE + 2 * GAP}"/>'
        "</mask></defs>"
        f'<g fill="none" stroke="url(#am-g)" stroke-width="{STROKE}" stroke-linejoin="round">'
        f'<path d="{rounded_svg_path(PATH, RADIUS)}" mask="url(#am-under)"/>'
        f'<path d="M{ax:.1f} {ay:.1f} L{bx:.1f} {by:.1f}" stroke-linecap="butt"/>'
        "</g></svg>\n"
    )


def gradient_color(x: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, (x - X0) / (X1 - X0)))
    for (o0, c0), (o1, c1) in zip(STOPS, STOPS[1:], strict=False):
        if t <= o1:
            u = (t - o0) / (o1 - o0)
            a = [int(c0[i : i + 2], 16) for i in (1, 3, 5)]
            b = [int(c1[i : i + 2], 16) for i in (1, 3, 5)]
            return tuple(round(a[k] + (b[k] - a[k]) * u) for k in range(3))
    return (0xFD, 0x42, 0x16)


def raster(size: int) -> Image.Image:
    """Square, transparent raster of the mark (8× supersampled)."""
    ss = 8
    big = size * ss
    vx, vy, vw, vh = VB
    scale = big / vw
    offset_y = (big - vh * scale) / 2

    def tr(p):
        return ((p[0] - vx) * scale, (p[1] - vy) * scale + offset_y)

    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    pts = [tr(p) for p in rounded_points(PATH, RADIUS)]
    width = max(1, round(STROKE * scale))
    draw.line(pts, fill=255, width=width, joint="curve")
    for p in pts:  # fill tiny gaps at joints
        r = width / 2
        draw.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=255)

    # over/under: cut a gap around the crossing, then draw the upper strand back on top
    from PIL import ImageChops

    (ax, ay), (bx, by) = crossing_segment()
    under = Image.new("L", (big, big), 0)
    ImageDraw.Draw(under).line([tr((ax, ay)), tr((bx, by))], fill=255, width=round((STROKE + 2 * GAP) * scale))
    over = Image.new("L", (big, big), 0)
    ImageDraw.Draw(over).line([tr((ax, ay)), tr((bx, by))], fill=255, width=width)
    mask = ImageChops.lighter(ImageChops.subtract(mask, under), over)

    grad = Image.new("RGB", (big, big))
    gdraw = ImageDraw.Draw(grad)
    for x in range(big):
        gdraw.line([(x, 0), (x, big)], fill=gradient_color(vx + x / scale))
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "logo.svg").write_text(svg(), encoding="utf-8")
    (PUBLIC / "favicon.svg").write_text(svg(title=False), encoding="utf-8")
    (ROOT / "docs" / "assets").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "assets" / "logo-mark.svg").write_text(svg(), encoding="utf-8")
    raster(32).save(PUBLIC / "favicon-32.png", optimize=True)
    raster(192).save(PUBLIC / "logo-192.png", optimize=True)
    raster(512).save(PUBLIC / "logo-512.png", optimize=True)
    raster(256).save(PUBLIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    for stale in ("logo-mark.png", "favicon.png"):
        (PUBLIC / stale).unlink(missing_ok=True)
    print("logo assets written to", PUBLIC)


if __name__ == "__main__":
    main()
