import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys, time, threading
from PIL import Image, ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import inject_scan

prefix = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "wave")
count = int(sys.argv[2]) if len(sys.argv) > 2 else 8
first = float(sys.argv[3]) if len(sys.argv) > 3 else 0.15

def inj():
    time.sleep(first)
    inject_scan.send_scan(0x72)
    print("inject at %.3f" % time.time(), flush=True)

threading.Thread(target=inj, daemon=True).start()
t0 = time.time()
for i in range(count):
    im = ImageGrab.grab()
    im.resize((im.width // 2, im.height // 2), Image.LANCZOS).save("%s_%02d.png" % (prefix, i))
    # 按钮区域（右侧 25% 高度）的特写
    im.crop((2620, 300, 2880, 620)).resize((390, 480), Image.LANCZOS).save("%s_btn_%02d.png" % (prefix, i))
    print("frame %02d  t=%.3f" % (i, time.time() - t0), flush=True)
