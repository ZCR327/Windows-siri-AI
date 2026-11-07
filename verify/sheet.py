import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys, glob, re
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pat = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "wave_*.png")
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "wave_sheet2.png")
cols = int(sys.argv[3]) if len(sys.argv) > 3 else 4
scale = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5

_m = re.compile(r"_(\d+)\.png$")
files = [p for p in glob.glob(pat) if _m.search(p)]
files = sorted(files, key=lambda p: int(_m.search(p).group(1)))
print("files: %d" % len(files))
if not files:
    raise SystemExit(0)
first = Image.open(files[0])
tw = int(first.width * scale)
th = int(first.height * scale)
rows = (len(files) + cols - 1) // cols
sheet = Image.new("RGB", (tw * cols, th * rows), (12, 12, 16))
for i, f in enumerate(files):
    im = Image.open(f).convert("RGB").resize((tw, th), Image.LANCZOS)
    sheet.paste(im, ((i % cols) * tw, (i // cols) * th))
sheet.save(out)
print("saved %s  %s" % (out, sheet.size))
