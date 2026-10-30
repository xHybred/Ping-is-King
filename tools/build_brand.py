"""Build the app icon and the GitHub banner from code.

Keeping the artwork as a script means the PNG/ICO files can be rebuilt after a
palette change instead of being edited by hand. The mark is deliberately
generic: a globe and a targeting reticle, no game artwork or logos.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "docs" / "brand"
FONTS = Path("C:/Windows/Fonts")

INK = (11, 18, 32)
PANEL = (17, 26, 43)
LINE = (36, 48, 68)
TEXT = (226, 232, 240)
MUTED = (148, 163, 184)
FAINT = (100, 116, 139)
TEAL = (45, 212, 191)
GREEN = (52, 211, 153)
RED = (248, 113, 113)
AMBER = (251, 191, 36)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def dashed_circle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
                  colour, width: int, dash: float = 14, gap: float = 12) -> None:
    step = math.degrees((dash + gap) / r)
    angle = 0.0
    while angle < 360:
        draw.arc(
            [cx - r, cy - r, cx + r, cy + r],
            start=angle,
            end=min(angle + math.degrees(dash / r), 360),
            fill=colour,
            width=width,
        )
        angle += step


def dashed_line(draw: ImageDraw.ImageDraw, start, end, colour, width: int,
                dash: float = 12, gap: float = 10) -> None:
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if not length:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos = 0.0
    while pos < length:
        stop = min(pos + dash, length)
        draw.line(
            [(x1 + ux * pos, y1 + uy * pos), (x1 + ux * stop, y1 + uy * stop)],
            fill=colour,
            width=width,
        )
        pos = stop + gap


def draw_mark(size: int, background: bool = True) -> Image.Image:
    """Crown over a reticle. Drawn at 4x then reduced so the edges stay smooth.

    The crown carries the "Ping is King" tagline; the ring keeps the server
    targeting idea readable at icon sizes.
    """
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if background:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=INK)
    c = s / 2
    ring = s * 0.335
    stroke = max(2, int(s * 0.026))

    d.ellipse([c - ring, c - ring, c + ring, c + ring], outline=MUTED, width=stroke)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        d.line(
            [
                c + dx * ring * 1.08,
                c + dy * ring * 1.08,
                c + dx * ring * 1.30,
                c + dy * ring * 1.30,
            ],
            fill=TEAL,
            width=stroke,
        )

    top = c - s * 0.155
    base = c + s * 0.170
    left = c - s * 0.220
    right = c + s * 0.220
    shoulder = c - s * 0.055
    valley = c + s * 0.035
    crown = [
        (left, base),
        (left, shoulder),
        (c - s * 0.110, valley),
        (c, top),
        (c + s * 0.110, valley),
        (right, shoulder),
        (right, base),
    ]
    d.polygon(crown, fill=TEAL)
    for px, py in ((left, shoulder), (c, top), (right, shoulder)):
        dot = s * 0.034
        d.ellipse([px - dot, py - dot, px + dot, py + dot], fill=TEAL)

    return img.resize((size, size), Image.LANCZOS)


def chip(draw, x, y, text, colour, fill, s, pad=14):
    f = font("segoeui.ttf", int(17 * s))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=f)
    w = (right - left) + pad * 2
    h = (bottom - top) + pad * 1.1
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, fill=fill)
    draw.text((x + pad, y + h / 2), text, font=f, fill=colour, anchor="lm")
    return w, h


def build_banner() -> Image.Image:
    s = 2
    w, h = 1280 * s, 640 * s
    img = Image.new("RGB", (w, h), INK)
    d = ImageDraw.Draw(img)

    # Quiet vertical wash and dot grid.
    for y in range(h):
        mix = y / h
        d.line(
            [(0, y), (w, y)],
            fill=(
                int(11 + 6 * mix),
                int(18 + 8 * mix),
                int(32 + 12 * mix),
            ),
        )
    for gx in range(0, w, 40 * s):
        for gy in range(0, h, 40 * s):
            d.point((gx, gy), fill=(22, 32, 50))

    mark = draw_mark(84, background=False)
    img.paste(mark, (72 * s, 66 * s), mark)
    title_f = font("seguisb.ttf", 34 * s)
    d.text((176 * s, 62 * s), "COD Ping King", font=title_f, fill=TEXT)
    d.text(
        (177 * s, 106 * s),
        "Ping is King",
        font=font("seguisb.ttf", 20 * s),
        fill=TEAL,
    )
    d.text(
        (177 * s, 138 * s),
        "Server filter for Call of Duty on Windows",
        font=font("segoeui.ttf", 15 * s),
        fill=MUTED,
    )

    d.text((72 * s, 206 * s), "Play on the servers", font=font("seguisb.ttf", 54 * s), fill=TEXT)
    d.text((72 * s, 270 * s), "you choose.", font=font("seguisb.ttf", 54 * s), fill=TEAL)

    bullets = [
        "Pick regions, or set a radius",
        "Block a server after a bad match",
        "Lobby and account traffic always allowed",
    ]
    by = 370 * s
    for line in bullets:
        d.ellipse([72 * s, by + 4 * s, 84 * s, by + 16 * s], fill=TEAL)
        d.text((100 * s, by), line, font=font("segoeui.ttf", 20 * s), fill=MUTED)
        by += 42 * s

    d.text(
        (72 * s, 548 * s),
        "Windows 10 / 11     Streamer mode",
        font=font("segoeui.ttf", 16 * s),
        fill=FAINT,
    )

    # Right panel: allowlist diagram.
    px1, py1, px2, py2 = 624 * s, 64 * s, 1208 * s, 576 * s
    d.rounded_rectangle([px1, py1, px2, py2], radius=26 * s, fill=PANEL, outline=LINE, width=2 * s)
    d.text(
        (660 * s, 96 * s),
        "HOW IT WORKS",
        font=font("seguisb.ttf", 15 * s),
        fill=FAINT,
    )

    home = (780 * s, 356 * s)
    dashed_circle(d, home[0], home[1], 152 * s, (45, 212, 191, 90), 2 * s)

    nodes = [
        ((988 * s, 214 * s), "Virginia", "28 ms", GREEN),
        ((1032 * s, 356 * s), "New York", "31 ms", GREEN),
        ((1096 * s, 470 * s), "California", "68 ms", RED),
    ]
    for (nx, ny), name, ping, colour in nodes:
        blocked = colour == RED
        if blocked:
            dashed_line(d, home, (nx, ny), RED, 2 * s, dash=10 * s, gap=8 * s)
        else:
            d.line([home, (nx, ny)], fill=GREEN, width=3 * s)
        rad = 13 * s
        d.ellipse([nx - rad, ny - rad, nx + rad, ny + rad], fill=colour)
        d.ellipse([nx - rad * 1.9, ny - rad * 1.9, nx + rad * 1.9, ny + rad * 1.9],
                  outline=colour, width=2 * s)
        d.text((nx + 24 * s, ny - 20 * s), name, font=font("seguisb.ttf", 17 * s), fill=TEXT)
        d.text((nx + 24 * s, ny + 2 * s), ping, font=font("segoeui.ttf", 15 * s), fill=colour)
        if blocked:
            d.line([nx - 8 * s, ny - 8 * s, nx + 8 * s, ny + 8 * s], fill=INK, width=3 * s)
            d.line([nx - 8 * s, ny + 8 * s, nx + 8 * s, ny - 8 * s], fill=INK, width=3 * s)

    hr = 22 * s
    d.ellipse([home[0] - hr, home[1] - hr, home[0] + hr, home[1] + hr], fill=TEAL)
    d.ellipse([home[0] - hr - 8 * s, home[1] - hr - 8 * s, home[0] + hr + 8 * s, home[1] + hr + 8 * s],
              outline=(45, 212, 191, 120), width=2 * s)
    d.text((home[0] - 10 * s, home[1] + 44 * s), "You", font=font("seguisb.ttf", 18 * s), fill=TEXT)
    d.text((home[0] - 10 * s, home[1] + 68 * s), "Home", font=font("segoeui.ttf", 15 * s), fill=MUTED)

    d.text(
        (660 * s, 514 * s),
        "Nearby servers stay. The rest are dropped.",
        font=font("segoeui.ttf", 17 * s),
        fill=MUTED,
    )
    return img.resize((1280, 640), Image.LANCZOS)


def build_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="COD Ping King">
  <rect width="512" height="512" rx="112" fill="#0b1220"/>
  <circle cx="256" cy="256" r="172" fill="none" stroke="#94a3b8" stroke-width="13"/>
  <g stroke="#2dd4bf" stroke-width="13" stroke-linecap="round">
    <path d="M256 66v34M256 412v34M66 256h34M412 256h34"/>
  </g>
  <polygon points="143,343 143,228 199,274 256,177 313,274 369,228 369,343" fill="#2dd4bf"/>
  <circle cx="143" cy="228" r="17" fill="#2dd4bf"/>
  <circle cx="256" cy="177" r="17" fill="#2dd4bf"/>
  <circle cx="369" cy="228" r="17" fill="#2dd4bf"/>
</svg>
"""


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    (ROOT / "packaging").mkdir(exist_ok=True)

    logo = draw_mark(1024)
    logo.save(BRAND / "logo.png")
    (BRAND / "logo.svg").write_text(build_svg(), encoding="utf-8")

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    logo.resize((256, 256), Image.LANCZOS).save(
        ROOT / "packaging" / "icon.ico",
        format="ICO",
        sizes=[(size, size) for size in ico_sizes],
    )

    build_banner().save(BRAND / "banner.png")
    print("brand assets written")


if __name__ == "__main__":
    main()
