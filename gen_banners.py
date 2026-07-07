# -*- coding: utf-8 -*-
"""Generates a small set of AI/tech-themed banner images (no external
dependency / API) to attach to Telegram posts instead of the auto-generated
link preview (which pulled the Chinese source page's own image/title)."""
import math
import random

from PIL import Image, ImageDraw, ImageFilter

W, H = 1200, 630

PALETTES = [
    # (bg_top, bg_bottom, accent1, accent2)
    ((10, 14, 35), (28, 20, 70), (99, 179, 237), (168, 122, 255)),
    ((8, 30, 40), (10, 60, 70), (56, 224, 196), (99, 179, 237)),
    ((25, 10, 45), (60, 15, 70), (255, 122, 200), (168, 122, 255)),
    ((6, 20, 30), (15, 45, 60), (72, 219, 251), (255, 195, 113)),
    ((15, 10, 30), (45, 15, 55), (129, 236, 236), (250, 130, 165)),
    ((5, 15, 25), (20, 40, 55), (0, 200, 150), (0, 140, 220)),
]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vertical_gradient(top, bottom):
    img = Image.new("RGB", (W, H), top)
    px = img.load()
    for y in range(H):
        t = y / H
        color = lerp(top, bottom, t)
        for x in range(W):
            px[x, y] = color
    return img


def draw_network(draw, accent1, accent2, seed):
    rnd = random.Random(seed)
    nodes = []
    for _ in range(22):
        x = rnd.randint(60, W - 60)
        y = rnd.randint(60, H - 60)
        nodes.append((x, y))

    # connections between nearby nodes
    for i, (x1, y1) in enumerate(nodes):
        for x2, y2 in nodes[i + 1:]:
            dist = math.hypot(x1 - x2, y1 - y2)
            if dist < 220:
                alpha = max(20, 120 - int(dist / 2))
                color = accent1 if (i % 2 == 0) else accent2
                draw.line([(x1, y1), (x2, y2)], fill=(*color, alpha), width=1)

    for (x, y) in nodes:
        r = rnd.randint(2, 5)
        color = accent1 if rnd.random() > 0.5 else accent2
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(*color, 220))


def draw_circuit_traces(draw, accent, seed):
    rnd = random.Random(seed + 99)
    for _ in range(14):
        x = rnd.randint(0, W)
        y = rnd.randint(0, H)
        length = rnd.randint(60, 180)
        horizontal = rnd.random() > 0.5
        if horizontal:
            end = (x + length, y)
        else:
            end = (x, y + length)
        draw.line([(x, y), end], fill=(*accent, 60), width=2)
        r = 4
        draw.ellipse([end[0] - r, end[1] - r, end[0] + r, end[1] + r], fill=(*accent, 140))


def build_banner(index: int, palette) -> Image.Image:
    bg_top, bg_bottom, accent1, accent2 = palette
    base = vertical_gradient(bg_top, bg_bottom).convert("RGBA")

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw_network(draw, accent1, accent2, seed=index * 7)
    draw_circuit_traces(draw, accent1, seed=index * 13)

    overlay = overlay.filter(ImageFilter.GaussianBlur(0.4))
    combined = Image.alpha_composite(base, overlay)

    # subtle vignette
    vignette = Image.new("L", (W, H), 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse([-200, -200, W + 200, H + 200], fill=60)
    vdraw.ellipse([150, 100, W - 150, H - 100], fill=0)
    vignette = vignette.filter(ImageFilter.GaussianBlur(120))
    black = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    combined = Image.composite(black, combined, vignette)

    return combined.convert("RGB")


def main():
    out_dir = r"D:\web site\telgram agent\assets"
    for i, palette in enumerate(PALETTES, start=1):
        img = build_banner(i, palette)
        path = f"{out_dir}\\topic_banner_{i}.jpg"
        img.save(path, quality=90)
        print("saved", path)


if __name__ == "__main__":
    main()
