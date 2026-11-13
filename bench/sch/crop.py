"""Crop a rendered sheet PNG (debugging aid): crop.py <png> <x0,y0,x1,y1 as fractions 0..1> <out png> [scale]"""
import sys
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
im = Image.open(sys.argv[1]).convert("RGB")
f = [float(v) for v in sys.argv[2].split(",")]
w, h = im.size
c = im.crop((int(f[0] * w), int(f[1] * h), int(f[2] * w), int(f[3] * h)))
k = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
if k != 1.0:
    c = c.resize((int(c.size[0] * k), int(c.size[1] * k)), Image.LANCZOS)
c.save(sys.argv[3])
print(c.size)
