import _bootstrap  # noqa: F401  (repo root on sys.path)
import sys, time
from PIL import Image, ImageGrab
prefix = sys.argv[1]
count = int(sys.argv[2]) if len(sys.argv) > 2 else 3
gap = float(sys.argv[3]) if len(sys.argv) > 3 else 1.6
for i in range(count):
    im = ImageGrab.grab()
    im.resize((im.width // 2, im.height // 2), Image.LANCZOS).save(r"%s_%d.png" % (prefix, i))
    print("frame", i, im.size)
    if i < count - 1:
        time.sleep(gap)
