"""Generate the WeChatAgentDesktop app icon.

Dark squircle + mint chat bubble + three dots + AI spark. Writes a
multi-resolution .ico and PNG previews under desktop/assets.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 2048
OUT = Path(__file__).resolve().parent / "assets"


def diagonal_gradient(size: int, start: tuple[int, int, int], end: tuple[int, int, int]) -> Image.Image:
    yy, xx = np.mgrid[0:size, 0:size]
    t = (xx + yy) / (2.0 * (size - 1))
    t = t[..., None]
    colors = np.array(start, dtype=np.float32) * (1 - t) + np.array(end, dtype=np.float32) * t
    return Image.fromarray(colors.astype(np.uint8), "RGB").convert("RGBA")


def vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    column = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None]
    colors = np.array(top, dtype=np.float32) * (1 - column) + np.array(bottom, dtype=np.float32) * column
    strip = Image.fromarray(np.repeat(colors[:, None, :], size, axis=1).astype(np.uint8), "RGB")
    return strip.convert("RGBA")


def squircle_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def bubble_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    left, top, right, bottom = int(size * 0.178), int(size * 0.230), int(size * 0.822), int(size * 0.668)
    radius = int(size * 0.142)
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=255)
    # Classic tail: attaches along the bottom edge, then drops down-left.
    tail = [
        (int(size * 0.372), int(size * 0.636)),
        (int(size * 0.300), int(size * 0.792)),
        (int(size * 0.512), int(size * 0.640)),
    ]
    draw.polygon(tail, fill=255)
    return mask


def star_mask(size: int, cx: int, cy: int, outer: int, inner: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    points = []
    for index in range(8):
        angle = -math.pi / 2 + index * math.pi / 4
        radius = outer if index % 2 == 0 else inner
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask


def masked(layer: Image.Image, mask: Image.Image) -> Image.Image:
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.paste(layer, (0, 0), mask)
    return out


def soft_highlight(size: int) -> Image.Image:
    """White sheen that fades from the top of the bubble."""
    layer = np.zeros((size, size, 4), dtype=np.uint8)
    yy = np.arange(size, dtype=np.float32)[:, None]
    start, end = size * 0.230, size * 0.500
    alpha = np.clip((end - yy) / (end - start), 0.0, 1.0) ** 1.6 * 70
    layer[..., 0:3] = 255
    layer[..., 3] = alpha.astype(np.uint8)
    return Image.fromarray(layer, "RGBA")


def soft_bottom_shade(size: int) -> Image.Image:
    layer = np.zeros((size, size, 4), dtype=np.uint8)
    yy = np.arange(size, dtype=np.float32)[:, None]
    start, end = size * 0.480, size * 0.668
    alpha = np.clip((yy - start) / (end - start), 0.0, 1.0) ** 1.4 * 44
    layer[..., 0:3] = np.array([0, 70, 60], dtype=np.uint8)
    layer[..., 3] = alpha.astype(np.uint8)
    return Image.fromarray(layer, "RGBA")


def compose() -> Image.Image:
    ground = diagonal_gradient(S, (26, 44, 56), (5, 9, 13))
    ground.putalpha(squircle_mask(S, int(S * 0.225)))

    # Soft teal glow behind the bubble.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (int(S * 0.10), int(S * 0.06), int(S * 0.94), int(S * 0.82)),
        fill=(45, 212, 191, 52),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(S * 0.11)))
    ground = Image.alpha_composite(ground, masked(glow, ground.getchannel("A")))

    body = bubble_mask(S)

    # Drop shadow under the bubble.
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 140), (0, 0), body)
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(S * 0.034)))
    shadow = shadow.transform(shadow.size, Image.Transform.AFFINE, (1, 0, 0, 0, 1, int(S * 0.020)))
    ground = Image.alpha_composite(ground, shadow)

    # Mint bubble with top sheen and bottom shade.
    bubble = masked(vertical_gradient(S, (150, 250, 232), (30, 198, 174)), body)
    bubble = Image.alpha_composite(bubble, masked(soft_highlight(S), body))
    bubble = Image.alpha_composite(bubble, masked(soft_bottom_shade(S), body))

    # Three dots.
    dots = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    dot_y, dot_r = int(S * 0.446), int(S * 0.033)
    for dot_x in (int(S * 0.350), int(S * 0.500), int(S * 0.650)):
        dd.ellipse((dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r), fill=(5, 38, 34, 242))
    bubble = Image.alpha_composite(bubble, dots)

    ground = Image.alpha_composite(ground, bubble)

    # AI spark at the top-right, clear of the bubble.
    spark = star_mask(S, int(S * 0.836), int(S * 0.238), int(S * 0.052), int(S * 0.014))
    spark_glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    spark_glow.paste((94, 234, 212, 170), (0, 0), spark)
    spark_glow = spark_glow.filter(ImageFilter.GaussianBlur(int(S * 0.016)))
    ground = Image.alpha_composite(ground, spark_glow)
    spark_solid = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    spark_solid.paste((238, 255, 252, 255), (0, 0), spark)
    ground = Image.alpha_composite(ground, spark_solid)

    # Thin rim light.
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(
        (1, 1, S - 2, S - 2),
        radius=int(S * 0.225),
        outline=(255, 255, 255, 30),
        width=max(2, int(S * 0.003)),
    )
    return Image.alpha_composite(ground, rim)


def main() -> None:
    master = compose().resize((1024, 1024), Image.Resampling.LANCZOS)
    OUT.mkdir(parents=True, exist_ok=True)
    master.save(OUT / "icon-1024.png")
    master.resize((256, 256), Image.Resampling.LANCZOS).save(OUT / "icon-256.png")
    master.resize((32, 32), Image.Resampling.LANCZOS).save(OUT / "icon-32.png")
    master.save(
        OUT / "WeChatAgentDesktop.ico",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)],
    )
    print("wrote", OUT / "WeChatAgentDesktop.ico")


if __name__ == "__main__":
    main()
