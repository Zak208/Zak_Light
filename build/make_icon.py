"""
Builds every image derived from build/logo.png (the original artwork: light "ZAK" lettering on black):

  build/zak_light.ico            icon of the .exe and of the installer
  src/web/static/favicon.ico     icon of the window (set at run time) and of the page
  src/web/static/logo.png        square mark shown in the app header
  src/web/static/logo-word.png   the lettering alone, white on transparent (the tray icon tints it)
  build/wizard-*.bmp             side and corner pictures of the installer wizard

The lettering is cut out of the black background by brightness, so it stays sharp at any size and on any colour.
Run it after replacing build/logo.png (crear_instalador.bat does it every time).
"""

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), "src", "web", "static")
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
BACKGROUND = (13, 14, 22, 255)       # the dark of the window
INK = (236, 238, 247)                # --text of the window


def lettering(path: str) -> Image.Image:
    """The big word of the artwork as a white-on-transparent image, cropped to the letters."""
    lum = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    alpha = np.clip((lum - 24.0) / (200.0 - 24.0), 0.0, 1.0)
    rows = np.where(alpha.max(axis=1) > 0.3)[0]
    # The artwork also has a small caption under the word: keep only the first block of rows
    gaps = np.where(np.diff(rows) > 12)[0]
    last = rows[gaps[0]] if len(gaps) else rows[-1]
    block = alpha[rows[0]:last + 1]
    cols = np.where(block.max(axis=0) > 0.3)[0]
    block = block[:, cols[0]:cols[-1] + 1]
    rgba = np.zeros(block.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = 255
    rgba[..., 3] = (block * 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def mark(word: Image.Image, size: int = 256) -> Image.Image:
    """Rounded dark square with the lettering centred: reads well from 16 px up."""
    scale = 4                                                   # draw large, reduce: smooth edges
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=BACKGROUND)
    w = int(big * 0.80)
    h = max(1, round(word.height * w / word.width))
    letters = word.resize((w, h), Image.LANCZOS)
    ink = Image.new("RGBA", letters.size, INK + (255,))
    ink.putalpha(letters.getchannel("A"))
    img.alpha_composite(ink, ((big - w) // 2, (big - h) // 2))
    return img.resize((size, size), Image.LANCZOS)


def wizard_images(icon: Image.Image):
    """Side picture (welcome / finish pages) and corner picture (the other pages) of the installer, at 1x and 2x."""
    def font(px):
        for name in ("segoeuib.ttf", "segoeui.ttf", "arialbd.ttf"):
            try:
                return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), px)
            except OSError:
                continue
        return ImageFont.load_default()

    for scale, suffix in ((1, ""), (2, "@2x")):
        w, h = 164 * scale, 314 * scale
        side = Image.new("RGB", (w, h), BACKGROUND[:3])
        px = side.load()
        for y in range(h):                                              # dark top to a hint of the brand purple
            t = y / (h - 1)
            for x in range(w):
                px[x, y] = (int(13 + 40 * t), int(14 + 12 * t), int(22 + 70 * t))
        size = 118 * scale
        side.paste(icon.resize((size, size), Image.LANCZOS), ((w - size) // 2, 70 * scale), icon.resize((size, size), Image.LANCZOS))
        d = ImageDraw.Draw(side)
        for text, px_size, y, colour in (("Zak_light", 22, 206, (236, 238, 247)), ("Iluminación ambiental", 11, 238, (164, 171, 194))):
            f = font(px_size * scale)
            d.text(((w - d.textlength(text, font=f)) / 2, y * scale), text, font=f, fill=colour)
        side.save(os.path.join(HERE, f"wizard-side{suffix}.bmp"))

        small = Image.new("RGB", (55 * scale, 55 * scale), (255, 255, 255))
        s = 47 * scale
        tile = icon.resize((s, s), Image.LANCZOS)
        small.paste(tile, ((55 * scale - s) // 2, (55 * scale - s) // 2), tile)
        small.save(os.path.join(HERE, f"wizard-small{suffix}.bmp"))


if __name__ == "__main__":
    word = lettering(os.path.join(HERE, "logo.png"))
    icon = mark(word, 256)
    os.makedirs(STATIC, exist_ok=True)
    for out in (os.path.join(HERE, "zak_light.ico"), os.path.join(STATIC, "favicon.ico")):
        icon.save(out, sizes=SIZES)
    icon.resize((128, 128), Image.LANCZOS).save(os.path.join(STATIC, "logo.png"), optimize=True)
    word.save(os.path.join(STATIC, "logo-word.png"), optimize=True)
    wizard_images(icon)
    print("icons ->", os.path.join(HERE, "zak_light.ico"), "and", STATIC)
