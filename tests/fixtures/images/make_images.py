"""Render the handwriting-like test images (run once; the PNGs are committed).

    uv run --with pillow python tests/fixtures/images/make_images.py

Uses macOS handwriting fonts (Bradley Hand / Chalkboard) when present, with per-line jitter and
rotation on ruled paper, so Gemini vision has to read 'handwriting' rather than clean type.
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
FONTS = ["/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
         "/System/Library/Fonts/Supplemental/Chalkboard.ttc",
         "/System/Library/Fonts/Supplemental/Chalkduster.ttf"]

SHEETS = {
    "order_fi_milk_butter.png": ["Tilaus / Hotel Aurora", "Maito laktoositon 200 l", "Voi 10 kg", "toimitus ma"],
    "school_oat_drink_fi.png": ["Koulukeittiö Tampere", "Kaurajuoma 120 l", "(maito- ja pähkinäallergia!)", "ke lounas"],
    "event_cream_butter_en.png": ["Conference dinner - Mon", "cream 40 L", "butter 20 kg", "all lactose-free"],
}


def font(path_idx: int, size: int):
    for p in FONTS[path_idx:] + FONTS[:path_idx]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(name: str, lines: list[str], seed: int, font_idx: int) -> None:
    rnd = random.Random(seed)
    w, h = 900, 120 + 110 * len(lines)
    img = Image.new("RGB", (w, h), (250, 247, 236))
    d = ImageDraw.Draw(img)
    for y in range(90, h, 110):  # ruled paper
        d.line([(30, y + 40), (w - 30, y + 40)], fill=(190, 205, 230), width=2)
    d.line([(80, 0), (80, h)], fill=(230, 170, 170), width=2)
    f = font(font_idx, 54)
    for i, text in enumerate(lines):
        layer = Image.new("RGBA", (w, 110), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.text((110 + rnd.randint(-8, 12), 18 + rnd.randint(-4, 4)), text, font=f,
                fill=(28 + rnd.randint(0, 20), 40, 110 + rnd.randint(0, 40), 255))
        layer = layer.rotate(rnd.uniform(-2.5, 2.5), resample=Image.BICUBIC)
        img.paste(layer, (0, 40 + i * 110), layer)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(HERE / name, optimize=True)


if __name__ == "__main__":
    for k, (name, lines) in enumerate(SHEETS.items()):
        render(name, lines, seed=11 + k, font_idx=k % len(FONTS))
        print("wrote", HERE / name)
