"""Generate icon.ico from the owner's sail-glyph sketch:
two masts with triangular sails on a hull baseline, black on white,
with "7seas telemetrics" lettered underneath. Drawn at 1024px and
downsampled for crisp small sizes.
"""

import os

from PIL import Image, ImageDraw, ImageFont

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=180,
                    fill=(250, 250, 250, 255), outline=(20, 20, 20, 255),
                    width=8)

# sketch source coordinates (from the provided drawing, ~1232x852 canvas,
# glyph bbox x 385-830 / y 103-706) mapped into the upper part of the icon
SRC = {
    "baseline": ((385, 632), (830, 632)),
    "mast1": ((563, 103), (563, 706)),
    "mast2": ((676, 103), (676, 706)),
    "sail1": ((455, 632), (563, 238)),
    "sail2": ((566, 632), (676, 242)),
}
gx0, gx1 = 385, 830
gy0, gy1 = 103, 706
box_w, box_h = 620, 560          # target glyph box inside the icon
scale = min(box_w / (gx1 - gx0), box_h / (gy1 - gy0))
ox = (S - (gx1 - gx0) * scale) / 2
oy = 90


def tr(p):
    return (ox + (p[0] - gx0) * scale, oy + (p[1] - gy0) * scale)


stroke = max(6, int(13 * scale))
for a, b in SRC.values():
    d.line([tr(a), tr(b)], fill=(15, 15, 15, 255), width=stroke)

# wordmark below the glyph
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def load_font(names, size):
    for n in names:
        p = os.path.join(FONT_DIR, n)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


f1 = load_font(("seguisb.ttf", "segoeui.ttf", "arialbd.ttf"), 108)
d.text((S / 2, 880), "7seas telemetrics", font=f1, fill=(15, 15, 15, 255),
       anchor="mm")

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                     (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(out.replace(".ico", "_preview.png"))
print("icon.ico written")
