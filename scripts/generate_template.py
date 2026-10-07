"""Draw the single predefined certificate background: templates/certificate_template.png.

Run once (the PNG is committed):  python scripts/generate_template.py

Only artwork is drawn here (frame, corner ornaments, rosette watermark, seal). All text is
drawn later by ReportLab, so it stays vector-sharp and searchable in the PDF.
"""

from math import cos, pi, sin
from pathlib import Path

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 1754, 1240  # A4 landscape at 150 DPI
SCALE = 2  # draw at 2x and downsample, for smooth anti-aliased edges
OUTPUT = Path(__file__).resolve().parents[1] / "templates" / "certificate_template.png"

CREAM = (253, 250, 242)
WATERMARK = (244, 238, 222)
NAVY = (31, 42, 68)
GOLD = (184, 145, 58)
LIGHT_GOLD = (214, 182, 104)
DARK_GOLD = (150, 114, 38)


def px(value: float) -> int:
    return round(value * SCALE)


def diamond(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, fill: tuple) -> None:
    draw.polygon(
        [(px(cx), px(cy - radius)), (px(cx + radius), px(cy)),
         (px(cx), px(cy + radius)), (px(cx - radius), px(cy))],
        fill=fill,
    )  # fmt: skip


def star_points(cx: float, cy: float, outer: float, inner: float, points: int) -> list:
    coords = []
    for index in range(points * 2):
        radius = outer if index % 2 == 0 else inner
        angle = -pi / 2 + index * pi / points
        coords.append((px(cx + radius * cos(angle)), px(cy + radius * sin(angle))))
    return coords


def draw_watermark(draw: ImageDraw.ImageDraw) -> None:
    """A faint guilloche-style rosette behind the text."""
    cx, cy = WIDTH / 2, HEIGHT / 2 - 20
    for index in range(36):
        angle = index * 2 * pi / 36
        ox, oy = cx + 120 * cos(angle), cy + 120 * sin(angle)
        draw.ellipse([px(ox - 250), px(oy - 250), px(ox + 250), px(oy + 250)],
                     outline=WATERMARK, width=px(1.5))  # fmt: skip


def draw_frame(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle([px(28), px(28), px(WIDTH - 28), px(HEIGHT - 28)], outline=NAVY, width=px(20))
    draw.rectangle([px(60), px(60), px(WIDTH - 60), px(HEIGHT - 60)], outline=GOLD, width=px(4))
    draw.rectangle([px(72), px(72), px(WIDTH - 72), px(HEIGHT - 72)],
                   outline=LIGHT_GOLD, width=px(2))  # fmt: skip

    for cx, cy, dx, dy in (
        (62, 62, 1, 1), (WIDTH - 62, 62, -1, 1),
        (62, HEIGHT - 62, 1, -1), (WIDTH - 62, HEIGHT - 62, -1, -1),
    ):  # fmt: skip
        # Corner brackets
        x0, y0 = cx + dx * 30, cy + dy * 30
        draw.line([px(x0), px(y0), px(x0 + dx * 140), px(y0)], fill=GOLD, width=px(3))
        draw.line([px(x0), px(y0), px(x0), px(y0 + dy * 140)], fill=GOLD, width=px(3))
        # Corner jewel
        diamond(draw, cx, cy, 24, GOLD)
        diamond(draw, cx, cy, 14, CREAM)
        diamond(draw, cx, cy, 7, NAVY)

    # Small ornament above the organisation name
    top = 150
    draw.line([px(WIDTH / 2 - 170), px(top), px(WIDTH / 2 - 26), px(top)], fill=GOLD, width=px(2))
    draw.line([px(WIDTH / 2 + 26), px(top), px(WIDTH / 2 + 170), px(top)], fill=GOLD, width=px(2))
    for offset, radius in ((-16, 5), (0, 9), (16, 5)):
        diamond(draw, WIDTH / 2 + offset, top, radius, GOLD)


def draw_seal(draw: ImageDraw.ImageDraw) -> None:
    """Gold seal centred between the date and signature lines."""
    cx, cy = WIDTH / 2, 990
    draw.polygon(star_points(cx, cy, 112, 101, 40), fill=GOLD)
    draw.ellipse([px(cx - 92), px(cy - 92), px(cx + 92), px(cy + 92)], fill=DARK_GOLD)
    draw.ellipse([px(cx - 84), px(cy - 84), px(cx + 84), px(cy + 84)], fill=GOLD)
    draw.ellipse([px(cx - 72), px(cy - 72), px(cx + 72), px(cy + 72)],
                 outline=CREAM, width=px(3))  # fmt: skip
    draw.polygon(star_points(cx, cy, 44, 18, 5), fill=CREAM)


def main() -> None:
    image = Image.new("RGB", (px(WIDTH), px(HEIGHT)), CREAM)
    draw = ImageDraw.Draw(image)
    draw_watermark(draw)
    draw_frame(draw)
    draw_seal(draw)
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, optimize=True, dpi=(150, 150))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
